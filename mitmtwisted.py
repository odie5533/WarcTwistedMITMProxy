# Copyright (c) David Bern

import argparse

from twisted.internet import ssl, reactor, protocol
from twisted.web._newclient import HTTPParser, ParseError, Request, \
    HTTPClientParser
from twisted.web.client import _URI

class ProxyProtocol(protocol.Protocol):
    def __init__(self, forward):
        self.forward = forward
        
    def dataReceived(self, data):
        print "ProxyProtocol dataReceived:", len(data)
        self.forward(data)

class WebProxyClientProtocol(HTTPClientParser):
    buffer = None
    serverProtocol = None

    def connectionLost(self, reason):
        print "Connection lost"
        if self.serverProtocol is not None:
            self.serverProtocol.transport.loseConnection()
            self.serverProtocol = None

    def connectionMade(self):
        self.proxyBodyProtocol = ProxyProtocol(self._forwardDataReceived)
        self.serverProtocol._resume(self)
        HTTPClientParser.connectionMade(self)
        
    def _forwardDataReceived(self, data):
        """ Takes raw data and forwards it right to the serverProtocol """
        self.serverProtocol.transport.write(data)
    
    def lineReceived(self, line):
        orig_line = line
        
        if line[-1:] == '\r':
            line = line[:-1]
        self._forwardDataReceived(line + '\r\n')
        
        HTTPClientParser.lineReceived(self, orig_line)
        
    def allHeadersReceived(self):
        print "WebProxyClientProtocol",self.headers
        HTTPClientParser.allHeadersReceived(self)
        self.response.deliverBody(self.proxyBodyProtocol)
        
class HTTP11WebProxyClientProtocol(protocol.Protocol):
    def newParser(self):
        self._parser = WebProxyClientProtocol(self.request, self.finished)
    def finished(self, rest):
        print "WebProxyClientFactory finished:",rest

class WebProxyClientFactory(protocol.ClientFactory):
    def __init__(self, serverProtocol, request):
        self.serverProtocol = serverProtocol
        self.request = request

    def buildProtocol(self, addr):
        # 2nd param is the finishResponse function:
        prot = WebProxyClientProtocol(self.request, lambda _: None)
        prot.serverProtocol = self.serverProtocol
        return prot

    def clientConnectionFailed(self, connector, reason):
        self.serverProtocol.transport.loseConnection()

class HTTPServerParser(HTTPParser):
    @staticmethod
    def parseContentLength(connHeaders):
        """ Parses the content length from connHeaders """
        contentLengthHeaders = connHeaders.getRawHeaders('content-length')
        if contentLengthHeaders is not None and len(contentLengthHeaders) == 1:
            return int(contentLengthHeaders[0])
        else:
            raise ValueError(
                          "Too many content-length headers; request is invalid")
        return None
    
    @staticmethod
    def convertUriToRelative(addr):
        """ Converts an absolute URI to a relative one """
        parsedURI = _URI.fromBytes(addr)
        parsedURI.scheme = parsedURI.netloc = None
        return parsedURI.toBytes()
        
    @staticmethod
    def requestFromHTTPHeaders(status, headers):
        """ Converts HTTP status + headers into a Request """
        parts = status.split(' ', 2)
        relative_uri = HTTPServerParser.convertUriToRelative(parts[1])
        return Request(parts[0], relative_uri, headers, None, persistent=True)
    
    def statusReceived(self, status):
        self.status = status
        
    def allHeadersReceived(self):
        parts = self.status.split(' ', 2)
        if len(parts) != 3:
            raise ParseError("wrong number of parts", self.status)
        method, request_uri, _ = parts
        if method != 'GET':
            self.contentLength = self.parseContentLength(self.connHeaders)
            if self.contentLength == 0:
                self._finished(self.clearLineBuffer())
            print "HTTPServerParser Header's Content length", self.contentLength
        # TOFIX: need to include a bodyProducer with the request
        # so that it knows to set a content-length
        self.requestParsed(
                         self.requestFromHTTPHeaders(self.status, self.headers))
        self.switchToBodyMode(None)
    
    def rawDataReceived(self, data):
        # TOFIX: use self.bodyDecoder to let HTTPParser handle the data INSTEAD
        # of handling raw data here
        # switchToBodyMode sets the bodyDecoder
        print "HTTPServerParser rawDataReceived:",len(data)
        self.bodyDataReceived(data)
    
    def requestParsed(self, request):
        """ Called with a request after it is parsed """
        pass
    
    def bodyDataReceived(self, data):
        """ Called when bodyData is received """
        pass
    
    def _finished(self):
        """ Called when the entire HTTP request is finished """
        pass

class WebProxyProtocol(HTTPParser):
    """ Creates a web proxy for HTTP and HTTPS """
    certinfo = { 'key':'ca.key', 'cert':'ca.crt' }
    serverParser = HTTPServerParser
    
    @staticmethod
    def parseHttpStatus(status):
        """ Returns (method, request_uri, http_version) """
        parts = status.split(' ', 2)
        if len(parts) != 3:
            raise ParseError("wrong number of parts", status)
        return parts
    
    @staticmethod
    def parseHostPort(addr, defaultPort=443):
        """ Parses 'host:port' into (host, port), given a defaultPort """
        port = defaultPort
        if b':' in addr:
            addr, port = addr.rsplit(b':')
            try:
                port = int(port)
            except ValueError:
                port = defaultPort
        return (addr, port)
    
    def __init__(self):
        self.useSSL = False
        self.clientProtocol = None
        self._rawDataBuffer = ''

    def statusReceived(self, status):
        self.status = status

    def rawDataReceived(self, data):
        """ Receives raw data from the proxied browser """
        print "WebProxy rawDataReceived:", len(data), ":"
        if self._serverParser:
            self._serverParser.dataReceived(data)
        else:
            # _rawDataBuffer is relayed when _resume() is called
            self._rawDataBuffer += data

    def _finished(self, rest):
        pass
    
    def writeToClientProtocol(self, data):
        """ Called after self._serverParser receives rawData """
        print "WebProxy writeToClientProtocol:", len(data)
        self.clientProtocol.transport.write(data)
        
    def requestParsed(self, request):
        """ Called after self.httpServerPareser parses a Request """
        print "WebProxyProtocol requestParsed"
        request.writeTo(self.clientProtocol.transport)
    
    def createHttpServerParser(self):
        self._serverParser = self.serverParser()
        self._serverParser.bodyDataReceived = self.writeToClientProtocol
        self._serverParser.requestParsed = self.requestParsed
        self._serverParser.connectionMade() # initializes instance vars
        
    def allHeadersReceived(self):
        """
        Parses the HTTP headers and starts a connection to the sever.
        After the connection is made, all data should come in raw (body mode)
        and should be sent to an HTTPServerParser
        """
        self.transport.pauseProducing()
        method, request_uri, _ = self.parseHttpStatus(self.status)
        
        self.useSSL = method == 'CONNECT'
        request = HTTPServerParser.requestFromHTTPHeaders(self.status,
                                                          self.headers)
        factory = WebProxyClientFactory(self, request)
        print "New connection"
        if self.useSSL:
            host, port = self.parseHostPort(request_uri, 443)
            ccf = ssl.ClientContextFactory()
            reactor.connectSSL(host, port, factory, ccf)
        else:
            # FIX: Should also check for host in the headers and not just the
            # status line
            uri = _URI.fromBytes(request_uri)
            reactor.connectTCP(uri.host, uri.port, factory)
        HTTPParser.allHeadersReceived(self) # self.switchToBodyMode(None)
    
    def _resume(self, clientProtocol):
        """
        Called when a connection to the remote server is established.
        Relay any extra data we received while waiting for the endpoint to
        connect, such as HTTP POST data
        """
        self.clientProtocol = clientProtocol
        
        self.createHttpServerParser()
        
        if not self.useSSL:
            # The outer header data for the SSL connection should not be parsed
            # These inject our already parsed data into the new _serverParser
            self._serverParser.status = self.status
            self._serverParser.headers = self.headers
            self._serverParser.connHeaders = self.connHeaders
            self._serverParser.allHeadersReceived()
        
        if len(self._rawDataBuffer) > 0:
            print "Spill-over data", len(self._rawDataBuffer)
            self._serverParser.dataReceived(self._rawDataBuffer)
            self._rawDataBuffer = ''
        
        self.transport.resumeProducing()
        if self.useSSL:
            self.transport.write('HTTP/1.0 200 Connection established\r\n\r\n')
            ctx = ssl.DefaultOpenSSLContextFactory(
                                    self.certinfo['key'], self.certinfo['cert'])
            self.transport.startTLS(ctx)
        

class MitmServerFactory(protocol.ServerFactory):
    protocol = WebProxyProtocol

def main():    
    parser = argparse.ArgumentParser(
                             description='Warc Man-in-the-Middle Twisted Proxy')
    parser.add_argument('-p', '--port', default='8000',
                        help='Port to run the proxy server on.')
    args = parser.parse_args()
    args.port = int(args.port)

    print "Proxy running on port", args.port
    reactor.listenTCP(args.port, MitmServerFactory())
    reactor.run()

if __name__=='__main__':
    main()
    #import autoreload
    #autoreload.main(main)
