# Copyright (c) David Bern

import autoreload
import sys
import socket
import urlparse
import argparse
from StringIO import StringIO

from twisted.internet import ssl, reactor, protocol
from twisted.python import log
from twisted.web._newclient import HTTPParser, ParseError, Request
from twisted.web.client import _URI

import warcrecords

class ClientProtocol(protocol.Protocol):
    buffer = None
    other = None

    def connectionLost(self, reason):
        if self.other is not None:
            self.other.transport.loseConnection()
            self.other = None

    def connectionMade(self):
        self.other.clientProtocol = self
        if self.request:
            self.request.writeTo(self.transport)

        # copied from t.p.portforward
        #self.transport.registerProducer(self.other.transport, True)
        #self.other.transport.registerProducer(self.transport, True)

        # re-start the inbound transport produder & ssl server mode
        self.other._resume()
        
    def dataReceived(self, data):
        """ Response data from the server is provided here """
        self.other.transport.write(data)

class ClientFactory(protocol.ClientFactory):
    request = None
    other = None

    def buildProtocol(self, addr):
        prot = ClientProtocol()
        prot.other = self.other
        prot.request = self.request
        return prot

    def clientConnectionFailed(self, connector, reason):
        self.other.transport.loseConnection()


class WebProxyProtocol(HTTPParser):
    certinfo = { 'key':'ca.key', 'cert':'ca.crt' }
    
    def __init__(self):
        self.useSSL = False
        self.clientProtocol = None
    def statusReceived(self, status):
        self.status = status
    def rawDataReceived(self, data):
        if self.clientProtocol:
            self.clientProtocol.transport.write(data)
        else:
            print "Proxy Server does not have access to the clientProtocol!"
            print "Received data is not being relayed to the upstream server!"
    def allHeadersReceived(self):
        self.transport.pauseProducing()
        parts = self.status.split(' ', 2)
        if len(parts) != 3:
            raise ParseError("wrong number of parts", status)
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
            forwardFactory = ClientFactory()
            forwardFactory.other = self
            reactor.connectSSL(host, port, forwardFactory, ccf)
        else:
            parsedURI = _URI.fromBytes(parts[1])
            # change url to be relative
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
            self.request = Request(parts[0], addr, self.headers, None)
            forwardFactory = ClientFactory()
            forwardFactory.other = self
            forwardFactory.request = self.request
            reactor.connectTCP(parsedURI.host, parsedURI.port, forwardFactory)
        HTTPParser.allHeadersReceived(self)
        
    def _resume(self):
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
    reactor.listenTCP(args.port, MitmServerFactory())
    reactor.run()

if __name__=='__main__':
    #main()
    autoreload.main(main)
