# Copyright (c) David Bern


# TOFIX: Handle Request bodies properly

import argparse

from twisted.internet import ssl, reactor, protocol
from twisted.web._newclient import HTTPParser, ParseError, Request, \
    HTTPClientParser
from twisted.web.client import _URI

from twisted.web.http import _DataLoss
class _RawChunkedTransferDecoder(object):
    """
    This class is modified from t.w.http._ChunkedTransferDecoder
    Rather than only returning the body chunks, this returns raw chunks
    but makes sure it stops at the end of the body.
    """
    state = 'CHUNK_LENGTH'

    def __init__(self, dataCallback, finishCallback):
        self.dataCallback = dataCallback
        self.finishCallback = finishCallback
        self._buffer = ''
        
    def _rawData(self, data):
        self.dataCallback(data)

    def _dataReceived_CHUNK_LENGTH(self, data):
        if '\r\n' in data:
            line, rest = data.split('\r\n', 1)
            self._rawData(line+'\r\n')
            parts = line.split(';')
            self.length = int(parts[0], 16)
            if self.length == 0:
                self.state = 'TRAILER'
            else:
                self.state = 'BODY'
            return rest
        else:
            self._buffer = data
            return ''

    def _dataReceived_CRLF(self, data):
        if data.startswith('\r\n'):
            self.state = 'CHUNK_LENGTH'
            self._rawData('\r\n')
            return data[2:]
        else:
            self._buffer = data
            return ''

    def _dataReceived_TRAILER(self, data):
        if data.startswith('\r\n'):
            self._rawData('\r\n')
            data = data[2:]
            self.state = 'FINISHED'
            self.finishCallback(data)
        else:
            self._buffer = data
        return ''

    def _dataReceived_BODY(self, data):
        if len(data) >= self.length:
            chunk, data = data[:self.length], data[self.length:]
            self._rawData(chunk)
            self.state = 'CRLF'
            return data
        elif len(data) < self.length:
            self.length -= len(data)
            self._rawData(data)
            return ''

    def _dataReceived_FINISHED(self, data):
        raise RuntimeError(
            "_ChunkedTransferDecoder.dataReceived called after last "
            "chunk was processed")


    def dataReceived(self, data):
        """
        Interpret data from a request or response body which uses the
        I{chunked} Transfer-Encoding.
        """
        data = self._buffer + data
        self._buffer = ''
        while data:
            data = getattr(self, '_dataReceived_%s' % (self.state,))(data)


    def noMoreData(self):
        """
        Verify that all data has been received.  If it has not been, raise
        L{_DataLoss}.
        """
        if self.state != 'FINISHED':
            raise _DataLoss(
                "Chunked decoder in %r state, still expecting more data to "
                "get to 'FINISHED' state." % (self.state,))

class ProxyProtocol(protocol.Protocol):
    """
    Takes in a function and calls that function with data whenever dataReceived
    is called
    """
    def __init__(self, forward):
        self.dataReceived = forward

class ProxyHTTPClientParser(HTTPClientParser):
    _transferDecoders = {
        'chunked': _RawChunkedTransferDecoder, # Use our Raw decoder
    }
    serverProtocol = None
    
    def forwardData(self, data):
        """ Takes raw data and forwards it right to the serverProtocol """
        raise NotImplementedError("Method must be overridden")
    
    def lineReceived(self, line):
        """ Forwards the headers exactly as they arrive """
        if line[-1:] == '\r':
            line = line[:-1]
        self.forwardData(line + '\r\n') 
        HTTPClientParser.lineReceived(self, line)
        
    def allHeadersReceived(self):
        HTTPClientParser.allHeadersReceived(self)
        self.response.deliverBody(ProxyProtocol(self.forwardData))
        
class HTTP11WebProxyClientProtocol(protocol.Protocol):
    """ HTTP11 creates new parsers as they are needed over the HTTP1.1 stream"""
    parser = ProxyHTTPClientParser
    
    def __init__(self, serverProtocol, con_uri):
        self.serverProtocol = serverProtocol
        self._buffer = ''
        self.connect_uri = con_uri
        
    def connectionMade(self):
        self.serverProtocol._resume(self)
        
    def connectionLost(self, reason):
        #print "HTTP11WebProxyClientProtocol Connection lost"
        if self.serverProtocol is not None:
            self.serverProtocol.transport.loseConnection()
            self.serverProtocol = None
    
    def dataFromClientParser(self, data):
        self.serverProtocol.transport.write(data)
    
    def newRequest(self, request):
        """
        Creates a new WebProxyHTTPClientParser parser with the given request
        """
        self.request = request
        self._parser = self.parser(request, self.finished)
        self._parser.forwardData = self.dataFromClientParser
        self._parser.makeConnection(self.transport)
        if self._buffer:
            self._parser.dataReceived(self._buffer)
            self._buffer = ''            
        
    def dataReceived(self, data):
        if self._parser is None:
            self._buffer += data
        else:
            self._parser.dataReceived(data)
        
    def _disconnectParser(self, reason):
        parser = self._parser
        self._parser = None
        parser.connectionLost(reason)
    
    def finished(self, rest):
        if rest:
            print "Spill-over data from the server:", len(rest)
            self._buffer += rest
        self._disconnectParser(None)

class WebProxyClientFactory(protocol.ClientFactory):
    protocol = HTTP11WebProxyClientProtocol
    
    def __init__(self, serverProtocol, con_uri):
        self.serverProtocol = serverProtocol
        self.con_uri = con_uri

    def buildProtocol(self, _):
        return self.protocol(self.serverProtocol, self.con_uri)

    def clientConnectionFailed(self, connector, reason):
        if self.serverProtocol is not None:
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
        
        if method == 'GET':
            self.contentLength = 0
        else:
            self.contentLength = self.parseContentLength(self.connHeaders)
            print "HTTPServerParser Header's Content length", self.contentLength
            # TOFIX: need to include a bodyProducer with the request
            # so that it knows to set a content-length
            self.switchToBodyMode(None)
        self.requestParsed(Request(method, request_uri, self.headers, None))
        if self.contentLength == 0:
            self._finished(self.clearLineBuffer())
            
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
    clientFactory = WebProxyClientFactory
    
    @staticmethod
    def convertUriToRelative(uri):
        """ Converts an absolute URI to a relative one """
        parsedURI = _URI.fromBytes(uri)
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
    
    def dataFromServerParser(self, data):
        """ Called after self._serverParser receives rawData """
        #print "WebProxyProtocol dataFromServerParser:", len(data)
        self.clientProtocol.transport.write(data)
        
    def requestParsed(self, request):
        """ Called after self._parser parses a Request """
        #print "  Request uri:",request.uri
        # Wikipedia does not accept absolute URIs:
        request.uri = self.convertUriToRelative(request.uri)
        # Check if any of the Connection connHeaders is 'close'
        conns = map(self._serverParser.connHeaders.getRawHeaders,
                    ['proxy-connection','connection'])
        hasClose = any([x.lower() == 'close' for y in conns if y for x in y])
        # HACK!!! Force close a connection if there is content-length because
        # I haven't implemented anything to check the length of the POST data
        request.persistent = \
            False if hasClose or self._serverParser.contentLength != 0 \
            else True
        self.clientProtocol.newRequest(request)
        request.writeTo(self.clientProtocol.transport)
    
    def createHttpServerParser(self):
        if self._serverParser is not None:
            self._serverParser.connectionLost(None)
            self._serverParser = None
        self._serverParser = self.serverParser(self.serverParserFinished)
        self._serverParser.rawDataReceived = self.dataFromServerParser
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
        connect = reactor.connectTCP
        if self.useSSL:
            request_uri = 'https://' + request_uri
            connect = lambda h,f,p: reactor.connectSSL(h, f, p,
                                                     ssl.ClientContextFactory())
        if request_uri[:4].lower() != 'http':
            # TOFIX: Should check for host in the headers and not just
            # the status line
            raise ParseError("HTTP status line did not have an absolute uri")
        
        parsedUri = _URI.fromBytes(request_uri)
        print "New connection to:", parsedUri.host, parsedUri.port
        connect(parsedUri.host, parsedUri.port,
                self.clientFactory(self, parsedUri.toBytes()))
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
            # Outer header data for the SSL connection should not be parsed
            # Since this is plain HTTP, these inject our already parsed data
            # into the new _serverParser
            self._serverParser.status = self.status
            self._serverParser.headers = self.headers
            self._serverParser.connHeaders = self.connHeaders
            self._serverParser.allHeadersReceived()
        
        if len(self._rawDataBuffer) > 0:
            print "Spill-over data", len(self._rawDataBuffer)
            self._serverParser.dataReceived(self._rawDataBuffer)
            self._rawDataBuffer = ''
        
        if self.useSSL:
            self.transport.write('HTTP/1.0 200 Connection established\r\n\r\n')
            ctx = ssl.DefaultOpenSSLContextFactory(
                                    self.certinfo['key'], self.certinfo['cert'])
            self.transport.startTLS(ctx)
        self.transport.resumeProducing()
        

class MitmServerFactory(protocol.ServerFactory):
    protocol = WebProxyProtocol

def main():    
    parser = argparse.ArgumentParser(
                             description='Twisted Man-in-the-Middle Proxy')
    parser.add_argument('-p', '--port', default='8080',
                        help='Port to run the proxy server on.')
    args = parser.parse_args()
    args.port = int(args.port)

    reactor.listenTCP(args.port, MitmServerFactory())
    print "Proxy running on port", args.port
    reactor.run()

if __name__=='__main__':
    main()
