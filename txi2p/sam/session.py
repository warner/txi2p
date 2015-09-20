# Copyright (c) str4d <str4d@mail.i2p>
# See COPYING for details.

from builtins import *
import os
from parsley import makeProtocol
from twisted.internet import defer, error
from twisted.python import failure, log

from txi2p import grammar
from txi2p.address import I2PAddress
from txi2p.sam import constants as c
from txi2p.sam.base import SAMSender, SAMReceiver, SAMFactory


class SessionCreateSender(SAMSender):
    def sendSessionCreate(self, style, id, privKey=None, options={}):
        msg = 'SESSION CREATE'
        msg += ' STYLE=%s' % style
        msg += ' ID=%s' % id
        msg += ' DESTINATION=%s' % (privKey if privKey else 'TRANSIENT')
        for key in options:
            msg += ' %s=%s' % (key, options[key])
        msg += '\n'
        self.transport.write(msg)


class SessionCreateReceiver(SAMReceiver):
    def command(self):
        if not (hasattr(self.factory, 'nickname') and self.factory.nickname):
            # All tunnels in the same process use the same nickname
            # TODO is using the PID a security risk?
            self.factory.nickname = 'txi2p-%d' % os.getpid()

        self.sender.sendSessionCreate(
            self.factory.style,
            self.factory.nickname,
            self.factory.privKey,
            self.factory.options)
        self.currentRule = 'State_create'

    def create(self, result, destination=None, message=None):
        if result != c.RESULT_OK:
            self.factory.resultNotOK(result, message)
            return

        self.factory.privKey = destination
        self.sender.sendNamingLookup('ME')
        self.currentRule = 'State_naming'

    def postLookup(self, dest):
        self.factory.sessionCreated(self, dest)


# A Protocol for making a SAM session
SessionCreateProtocol = makeProtocol(
    grammar.samGrammarSource,
    SessionCreateSender,
    SessionCreateReceiver)


class SessionCreateFactory(SAMFactory):
    protocol = SessionCreateProtocol

    def __init__(self, nickname, style='STREAM', keyfile=None, options={}):
        if style != 'STREAM':
            raise error.UnsupportedSocketType()
        self.nickname = nickname
        self.style = style
        self._keyfile = keyfile
        self.options = options
        self.deferred = defer.Deferred(self._cancel)
        self.samVersion = None
        self.privKey = None
        self._writeKeypair = False

    def startFactory(self):
        if self._keyfile:
            try:
                f = open(self._keyfile, 'r')
                self.privKey = f.read()
                f.close()
            except IOError:
                log.msg('Could not load private key from %s' % self._keyfile)
                self._writeKeypair = True

    def sessionCreated(self, proto, pubKey):
        if self._writeKeypair:
            try:
                f = open(self._keyfile, 'w')
                f.write(self.privKey)
                f.close()
            except IOError:
                log.msg('Could not save private key to %s' % self._keyfile)
        # Help keep the session open
        try:
            proto.sender.transport.setTcpKeepAlive(1)
        except AttributeError as e:
            log.msg(e)
        # Now continue on with creation of SAMSession
        self.deferred.callback((self.samVersion, self.style, self.nickname, proto, pubKey))


# Dictionary containing all active SAM sessions
_sessions = {}


class SAMSession(object):
    """A SAM session represents an active I2P Destination.

    Attributes:
        nickname (str): The user-assigned session nickname, can be `None`.
        samEndpoint (twisted.internet.interfaces.IStreamClientEndpoint): An
            endpoint that will connect to the SAM API.
        samVersion (str): The SAM version in use by this session.
        style (str): The session style.
        id (str): SAM Session ID, autogenerated if ``nickname`` is None, else
            ``nickname``.
        address (txi2p.I2PAddress): The Destination of this session.
    """

    def __init__(self):
        self.nickname = None
        self.samEndpoint = None
        self.samVersion = ''
        self.style = 'STREAM'
        self.id = None
        self.address = None
        self._proto = None
        self._autoClose = False
        self._closed = False
        self._streams = []

    def addStream(self, stream):
        """Register a stream with this session.

        Raises:
            twisted.internet.error.ConnectionDone: if the session is closed.
        """
        if self._closed:
            raise error.ConnectionDone
        self._streams.append(stream)

    def removeStream(self, stream):
        """Remove a stream from this session.

        If this was the last stream, and the session was set to auto-close, the
        session will close and become unusable.

        Raises:
            twisted.internet.error.ConnectionDone: if the session is closed.
        """
        if self._closed:
            raise error.ConnectionDone
        # Streams are only added once they have been established
        if stream in self._streams:
            self._streams.remove(stream)
        if not self._streams and self._autoClose:
            # No more streams, close the session
            self.close()

    def close(self):
        """Close the session."""
        self._closed = True
        self._streams = []
        self._proto.sender.transport.loseConnection()
        del _sessions[self.nickname]


def getSession(nickname, samEndpoint=None, autoClose=False, **kwargs):
    """Get or create a SAM session.

    Args:
        nickname (str): The session nickname.
        samEndpoint (twisted.internet.interfaces.IStreamClientEndpoint): An
            endpoint that will connect to the SAM API.
        autoClose (bool): `true` if the session should close automatically once
            no more connections are using it.
    """
    if nickname in _sessions:
        return defer.succeed(_sessions[nickname])

    if not samEndpoint:
        raise ValueError('A new session cannot be created without an API Endpoint')

    def createSession(vals):
        samVersion, style, id, proto, pubKey = vals
        s = SAMSession()
        s.nickname = nickname
        s.samEndpoint = samEndpoint
        s.samVersion = samVersion
        s.style = style
        s.id = id
        s.address = I2PAddress(pubKey)
        s._proto = proto
        s._autoClose = autoClose
        _sessions[nickname] = s
        return s

    sessionFac = SessionCreateFactory(nickname, **kwargs)
    d = samEndpoint.connect(sessionFac)
    # Force caller to wait until the session is actually created
    d.addCallback(lambda proto: sessionFac.deferred)
    d.addCallback(createSession)
    return d


class DestGenerateSender(SAMSender):
    def sendDestGenerate(self):
        self.transport.write('DEST GENERATE\n')


class DestGenerateReceiver(SAMReceiver):
    def command(self):
        self.sender.sendDestGenerate()
        self.currentRule = 'State_dest'

    def destGenerated(self, pub, priv):
        self.factory.destGenerated(pub, priv)
        self.sender.transport.loseConnection()


# A Protocol for generating an I2P Destination via SAM
DestGenerateProtocol = makeProtocol(
    grammar.samGrammarSource,
    DestGenerateSender,
    DestGenerateReceiver)


class DestGenerateFactory(SAMFactory):
    protocol = DestGenerateProtocol

    def __init__(self, keyfile):
        self._keyfile = keyfile
        self.deferred = defer.Deferred(self._cancel)

    def destGenerated(self, pubKey, privKey):
        if os.path.exists(self._keyfile):
            self.deferred.errback(failure.Failure(ValueError('The keyfile already exists')))
            return

        try:
            f = open(self._keyfile, 'w')
            f.write(privKey)
            f.close()
            self.deferred.callback(I2PAddress(pubKey))
        except IOError as e:
            self.deferred.errback(failure.Failure(e))


def generateDestination(keyfile, samEndpoint):
    """Generate a new I2P Destination.

    The function returns a :class:`twisted.internet.defer.Deferred`; register
    callbacks to receive the return value or errors.

    Args:
        keyfile (str): Path to a local file where the keypair for the new
            Destination should be stored.
        samEndpoint (twisted.internet.interfaces.IStreamClientEndpoint): An
            endpoint that will connect to the SAM API.

    Returns:
        txi2p.I2PAddress: The new Destination. Once this is received via the
        Deferred callback, the ``keyfile`` will have been written.

    Raises:
        ValueError: if the ``keyfile`` already exists.
        IOError: if the ``keyfile`` write fails.
    """
    destFac = DestGenerateFactory(keyfile)
    d = samEndpoint.connect(destFac)
    d.addCallback(lambda proto: destFac.deferred)
    return d
