# Copyright (c) David Bern

import urlparse
import argparse

from twisted.internet import ssl, reactor, protocol
from twisted.web._newclient import HTTPParser, ParseError, Request
from twisted.web.client import _URI

class ClientProtocol(protocol.Protocol):
    buffer = None
    serverProtocol = None

    def connectionLost(self, reason):
        if self.serverProtocol is not None:
            self.serverProtocol.transport.loseConnection()
            self.serverProtocol = None

    def connectionMade(self):
        self.serverProtocol.clientProtocol = self
        if self.request:
            self.request.writeTo(self.transport)

        # copied from t.p.portforward
        #self.transport.registerProducer(self.serverProtocol.transport, True)
        #self.serverProtocol.transport.registerProducer(self.transport, True)

        # re-start the inbound transport produder & ssl server mode
        self.serverProtocol._resume()
        
    def dataReceived(self, data):
        """ Response data from the server is provided here """
        self.serverProtocol.transport.write(data)

class ClientFactory(protocol.ClientFactory):
    request = None
    serverProtocol = None

    def buildProtocol(self, addr):
        prot = ClientProtocol()
        prot.serverProtocol = self.serverProtocol
        prot.request = self.request
        return prot

    def clientConnectionFailed(self, connector, reason):
        self.serverProtocol.transport.loseConnection()

class HTTPServerParser(HTTPParser):
    def statusReceived(self, status):
        self.status = status
    def allHeadersReceived(self):
        parts = self.status.split(' ', 2)
        if len(parts) != 3:
            raise ParseError("wrong number of parts", self.status)
        parsedURI = _URI.fromBytes(parts[1])
        # change url to be relative:
        addr = urlparse.urlunparse((None, None, parsedURI.path,
                     parsedURI.params, parsedURI.query, parsedURI.fragment))
        if parts[0] != 'GET':
            contentLengthHeaders = self.connHeaders.getRawHeaders('content-length')
            if contentLengthHeaders is None:
                contentLength = None
            elif len(contentLengthHeaders) == 1:
                contentLength = int(contentLengthHeaders[0])
            else:
                raise ValueError(
                      "Too many content-length headers; request is invalid")
            self.contentLength = contentLength
            if contentLength == 0:
                self._finished(self.clearLineBuffer())
            print contentLength
        self.requestParsed(Request(parts[0], addr, self.headers, None))
    def requestParsed(self, request):
        pass

class WebProxyProtocol(HTTPParser):
    certinfo = { 'key':'ca.key', 'cert':'ca.crt' }
    serverParser = HTTPServerParser
    
    def __init__(self):
        self.useSSL = False
        self.clientProtocol = None
        self._rawDataBuffer = ''

    def statusReceived(self, status):
        self.status = status
    def lineReceived(self, line):
        print line
        HTTPParser.lineReceived(self, line)
    # Receiving raw data from the proxied browser
    def rawDataReceived(self, data):
        print len(data)
        if self.clientProtocol:
            self.clientProtocol.transport.write(data)
        else:
            self._rawDataBuffer += data # data is relayed when _resume is called
    def _finished(self, rest):
        #self.finisher(rest)
        pass
    def allHeadersReceived(self):
        self.transport.pauseProducing()
        parts = self.status.split(' ', 2)
        if len(parts) != 3:
            raise ParseError("wrong number of parts", self.status)
        forwardFactory = ClientFactory()
        forwardFactory.serverProtocol = self
        addr = parts[1]
        if parts[0] == 'CONNECT':
            print "New SSL connection"
            self.useSSL = True
            host = addr
            port = 443
            if b':' in addr:
                host, port = addr.rsplit(':',1)
            port = int(port)
            ccf = ssl.ClientContextFactory()
            reactor.connectSSL(host, port, forwardFactory, ccf)  # @UndefinedVariable
        else:
            parsedURI = _URI.fromBytes(parts[1])
            # change url to be relative:
            addr = urlparse.urlunparse((None, None, parsedURI.path,
                         parsedURI.params, parsedURI.query, parsedURI.fragment))
            if parts[0] != 'GET':
                contentLengthHeaders = self.connHeaders.getRawHeaders('content-length')
                if contentLengthHeaders is None:
                    contentLength = None
                elif len(contentLengthHeaders) == 1:
                    contentLength = int(contentLengthHeaders[0])
                else:
                    raise ValueError(
                          "Too many content-length headers; request is invalid")
                self.contentLength = contentLength
                if contentLength == 0:
                    self._finished(self.clearLineBuffer())
                print contentLength
            request = Request(parts[0], addr, self.headers, None)
            self.requestParsed(request)
            forwardFactory.request = request
            reactor.connectTCP(parsedURI.host, parsedURI.port, forwardFactory)  # @UndefinedVariable
        HTTPParser.allHeadersReceived(self)
        
        self.requestParsed(Request(parts[0], addr, self.headers, None))
    def requestParsed(self, request):
        pass
    
    def _resume(self):
        # Relay any extra data we received while waiting for the endpoint to
        # connect, such as HTTP POST data
        self.clientProtocol.transport.write(self._rawDataBuffer)
        self._rawDataBuffer = ''
        self.transport.resumeProducing()
        if self.useSSL:
            self.transport.write('HTTP/1.0 200 Connection established\r\n\r\n')
            ctx = ssl.DefaultOpenSSLContextFactory(self.certinfo['key'], self.certinfo['cert'])
            self.transport.startTLS(ctx)
            

class MitmServerFactory(protocol.ServerFactory):
    protocol = WebProxyProtocol

def main():    
    parser = argparse.ArgumentParser(description='Warc Man-in-the-Middle Twisted Proxy')
    parser.add_argument('-p', '--port', default='8000',
                        help='Port to run the proxy server on.')
    args = parser.parse_args()
    args.port = int(args.port)

    print "Proxy running on port", args.port
    reactor.listenTCP(args.port, MitmServerFactory())  # @UndefinedVariable
    reactor.run()  # @UndefinedVariable

if __name__=='__main__':
    main()
    #import autoreload
    #autoreload.main(main)
