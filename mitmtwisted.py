# Copyright (c) David Bern

import argparse

from twisted.internet import ssl, reactor, protocol
from twisted.web._newclient import HTTPParser, ParseError, Request, \
    HTTPClientParser
from twisted.web.client import _URI

class ProxyProtocol(protocol.Protocol):
    """
    Takes in a function and calls that function with data whenever dataReceived
    is called
    """
    def __init__(self, forward):
        self.dataReceived = forward

class WebProxyClientProtocol(HTTPClientParser):
    serverProtocol = None
    
    def _forwardData(self, data):
        """ Takes raw data and forwards it right to the serverProtocol """
        self.serverProtocol.transport.write(data)
    
    def lineReceived(self, line):
        """ Forwards the headers """
        if line[-1:] == '\r':
            line = line[:-1]
        self._forwardData(line + '\r\n')        
        HTTPClientParser.lineReceived(self, line)
        
    def allHeadersReceived(self):
        HTTPClientParser.allHeadersReceived(self)
        self.response.deliverBody(ProxyProtocol(self._forwardData))
        
class HTTP11WebProxyClientProtocol(protocol.Protocol):
    """ HTTP11 creates new parsers as they are needed over the HTTP1.1 stream"""
    def __init__(self, serverProtocol):
        self.serverProtocol = serverProtocol
        
    def connectionMade(self):
        self.serverProtocol._resume(self)
        
    def connectionLost(self, reason):
        #print "HTTP11WebProxyClientProtocol Connection lost"
        if self.serverProtocol is not None:
            self.serverProtocol.transport.loseConnection()
            self.serverProtocol = None
    
    def newRequest(self, request):
        self._parser = WebProxyClientProtocol(request, self.finished)
        self._parser.serverProtocol = self.serverProtocol
        self._parser.makeConnection(self.transport)
        
    def dataReceived(self, data):
        if self._parser is None:
            raise ParseError("HTTP11WebProxyClientProtocol has no _parser. Please issue a newRequest")
        self._parser.dataReceived(data)
        
    def _disconnectParser(self, reason):
        parser = self._parser
        self._parser = None
        parser.connectionLost(reason)
    
    def finished(self, rest):
        #print "HTTP11WebProxyClientProtocol finished:",len(rest)
        assert len(rest) == 0
        self._disconnectParser(None)

class WebProxyClientFactory(protocol.ClientFactory):
    def __init__(self, serverProtocol):
        self.serverProtocol = serverProtocol

    def buildProtocol(self, addr):
        return HTTP11WebProxyClientProtocol(self.serverProtocol)

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
    
    def __init__(self, finisher):
        self.finisher = finisher
    
    def statusReceived(self, status):
        self.status = status
        
    def allHeadersReceived(self):
        parts = self.status.split(' ', 2)
        if len(parts) != 3:
            raise ParseError("wrong number of parts", self.status)
        method, request_uri, _ = parts
        
        self.requestParsed(Request(method, request_uri, self.headers, None,
                          persistent=True))
        
        if method == 'GET':
            self.contentLength = 0
            self._finished(self.clearLineBuffer())
        else:
            self.contentLength = self.parseContentLength(self.connHeaders)
            if self.contentLength == 0:
                self._finished(self.clearLineBuffer())
            print "HTTPServerParser Header's Content length", self.contentLength
            # TOFIX: need to include a bodyProducer with the request
            # so that it knows to set a content-length
            self.switchToBodyMode(None)
            
    def _finished(self, rest):
        """ Called when the entire HTTP request + body is finished """
        #print "HTTPServerParser _finished:", len(rest)
        assert len(rest) == 0
        self.finisher(rest)
    
    def requestParsed(self, request):
        """ Called with a request after it is parsed """
        pass

class WebProxyProtocol(HTTPParser):
    """ Creates a web proxy for HTTP and HTTPS """
    certinfo = { 'key':'ca.key', 'cert':'ca.crt' }
    serverParser = HTTPServerParser
    
    @staticmethod
    def convertUriToRelative(addr):
        """ Converts an absolute URI to a relative one """
        parsedURI = _URI.fromBytes(addr)
        parsedURI.scheme = parsedURI.netloc = None
        return parsedURI.toBytes()
    
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
        self._serverParser = None

    def statusReceived(self, status):
        self.status = status

    def rawDataReceived(self, data):
        """ Receives raw data from the proxied browser """
        #print "WebProxyProtocol rawDataReceived:", len(data), ":"
        if self._serverParser is not None:
            self._serverParser.dataReceived(data)
        else:
            # _rawDataBuffer is relayed when _resume() is called
            self._rawDataBuffer += data
    
    def writeToClientProtocol(self, data):
        """ Called after self._serverParser receives rawData """
        #print "WebProxyProtocol writeToClientProtocol:", len(data)
        self.clientProtocol.transport.write(data)
        
    def requestParsed(self, request):
        """ Called after self.httpServerPareser parses a Request """
        print "  Request headers:", request.headers
        # Wikipedia does not accept absolute URIs:
        request.uri = self.convertUriToRelative(request.uri)
        self.clientProtocol.newRequest(request)
        request.writeTo(self.clientProtocol.transport)
    
    def createHttpServerParser(self):
        if self._serverParser is not None:
            self._serverParser.connectionLost(None)
            self._serverParser = None
        self._serverParser = self.serverParser(self.serverParserFinished)
        self._serverParser.rawDataReceived = self.writeToClientProtocol
        self._serverParser.requestParsed = self.requestParsed
        self._serverParser.connectionMade() # initializes instance vars
        
    def serverParserFinished(self, rest):
        assert len(rest) == 0
        self.createHttpServerParser()
        
    def allHeadersReceived(self):
        """
        Parses the HTTP headers and starts a connection to the sever.
        After the connection is made, all data should come in raw (body mode)
        and should be sent to an HTTPServerParser
        """
        self.transport.pauseProducing()
        method, request_uri, _ = self.parseHttpStatus(self.status)
        
        self.useSSL = method == 'CONNECT'
        if self.useSSL:
            host, port = self.parseHostPort(request_uri, 443)
            ccf = ssl.ClientContextFactory()
            print "New SSL:",host,port
            reactor.connectSSL(host, port, WebProxyClientFactory(self), ccf)
        else:
            if request_uri[:4].lower() == 'http':
                uri = _URI.fromBytes(request_uri)
            else:
                # TOFIX: Should check for host in the headers and not just
                # the status line
                raise ParseError('Status line did not contain an absolute uri!')
            print "New HTTP:",uri.host,uri.port
            reactor.connectTCP(uri.host, uri.port, WebProxyClientFactory(self))
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
