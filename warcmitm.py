# Copyright (c) David Bern


import argparse

from twisted.internet import reactor
from twisted.web.client import _URI

import warcrecords
from mitmtwisted import MitmServerFactory, WebProxyProtocol,\
        WebProxyClientFactory, HTTP11WebProxyClientProtocol

class WarcOutputSingleton(object):
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(WarcOutputSingleton, cls).__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self, filename=None):
        # Make sure init is not called more than once
        try:
            self.__fo
        except AttributeError:
            if filename is None:
                filename = "out.warc.gz"
                print "WarcOutput was not given a filename. Using", filename
            self.use_gzip = filename.endswith('.gz')
            self.__fo = open(filename, 'wb')
            record = warcrecords.WarcinfoRecord()
            record.write_to(self.__fo, gzip=self.use_gzip)

    # Write a given record to the output file
    def write_record(self, record):
        record.write_to(self.__fo, gzip=self.use_gzip)
        
def _copy_attrs(to, frum, attrs):
    map(lambda a: setattr(to, a, getattr(frum, a)), attrs)

class WarcHTTP11WebProxyClientProtocol(HTTP11WebProxyClientProtocol):    
    def dataFromClientParser(self, data):
        try:
            self._bodyBuffer += data
        except AttributeError:
            self._bodyBuffer = data
        HTTP11WebProxyClientProtocol.dataFromClientParser(self, data)
    
    def getRecordUri(self):
        req_uri = _URI.fromBytes(self.request.uri)
        con_uri = _URI.fromBytes(self.connect_uri)
        # Remove default port from URL
        if con_uri.port == (80 if con_uri.scheme == 'http' else 443):
            con_uri.netloc = con_uri.host
        # Copy parameters from the relative req_uri to the con_uri
        _copy_attrs(con_uri, req_uri, ['path','params','query','fragment'])
        return con_uri.toBytes()
    
    def finished(self, rest):
        # Write out Response record to WARC
        record = warcrecords.WarcResponseRecord(url=self.getRecordUri(),
                                                block=self._bodyBuffer)
        self._bodyBuffer = ''
        WarcOutputSingleton().write_record(record)
        HTTP11WebProxyClientProtocol.finished(self, rest)
    
    def newRequest(self, request):
        HTTP11WebProxyClientProtocol.newRequest(self, request)

class WarcWebProxyClientFactory(WebProxyClientFactory):
    protocol = WarcHTTP11WebProxyClientProtocol

class WarcWebProxyProtocol(WebProxyProtocol):
    clientFactory = WarcWebProxyClientFactory
    
    def dataFromServerParser(self, data):
        WebProxyProtocol.dataFromServerParser(self, data)
    def createHttpServerParser(self):
        WebProxyProtocol.createHttpServerParser(self)
    def requestParsed(self, request):
        WebProxyProtocol.requestParsed(self, request)

class WarcMitmServerFactory(MitmServerFactory):
    protocol = WarcWebProxyProtocol

def main():    
    parser = argparse.ArgumentParser(
                             description='Warc Twisted Man-in-the-Middle Proxy')
    parser.add_argument('-p', '--port', default='8080',
                        help='Port to run the proxy server on.')
    parser.add_argument('-f', '--file', default='out.warc.gz',
                        help='WARC file to output to')
    args = parser.parse_args()
    args.port = int(args.port)

    reactor.listenTCP(args.port, WarcMitmServerFactory())
    WarcOutputSingleton(args.file)
    print "Proxy running on port", args.port
    reactor.run()

if __name__=='__main__':
    main()
