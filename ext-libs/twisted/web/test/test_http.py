# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Test HTTP support.
"""

import random, cgi, base64

try:
    from urlparse import (
        ParseResult as ParseResultBytes, urlparse, urlunsplit, clear_cache)
except ImportError:
    from urllib.parse import (
        ParseResultBytes, urlparse, urlunsplit, clear_cache)

from twisted.python.compat import _PY3, iterbytes, networkString, unicode, intToBytes
from twisted.python.failure import Failure
from twisted.trial import unittest
from twisted.trial.unittest import TestCase
from twisted.web import http, http_headers
from twisted.web.http import PotentialDataLoss, _DataLoss
from twisted.web.http import _IdentityTransferDecoder
from twisted.internet.task import Clock
from twisted.internet.error import ConnectionLost
from twisted.protocols import loopback
from twisted.test.proto_helpers import StringTransport
from twisted.test.test_internet import DummyProducer
from twisted.web.test.requesthelper import DummyChannel




class DateTimeTest(unittest.TestCase):
    """Test date parsing functions."""

    def testRoundtrip(self):
        for i in range(10000):
            time = random.randint(0, 2000000000)
            timestr = http.datetimeToString(time)
            time2 = http.stringToDatetime(timestr)
            self.assertEqual(time, time2)


class DummyHTTPHandler(http.Request):

    def process(self):
        self.content.seek(0, 0)
        data = self.content.read()
        length = self.getHeader(b'content-length')
        if length is None:
            length = networkString(str(length))
        request = b"'''\n" + length + b"\n" + data + b"'''\n"
        self.setResponseCode(200)
        self.setHeader(b"Request", self.uri)
        self.setHeader(b"Command", self.method)
        self.setHeader(b"Version", self.clientproto)
        self.setHeader(b"Content-Length", intToBytes(len(request)))
        self.write(request)
        self.finish()


class LoopbackHTTPClient(http.HTTPClient):

    def connectionMade(self):
        self.sendCommand(b"GET", b"/foo/bar")
        self.sendHeader(b"Content-Length", 10)
        self.endHeaders()
        self.transport.write(b"0123456789")


class ResponseTestMixin(object):
    """
    A mixin that provides a simple means of comparing an actual response string
    to an expected response string by performing the minimal parsing.
    """

    def assertResponseEquals(self, responses, expected):
        """
        Assert that the C{responses} matches the C{expected} responses.

        @type responses: C{bytes}
        @param responses: The bytes sent in response to one or more requests.

        @type expected: C{list} of C{tuple} of C{bytes}
        @param expected: The expected values for the responses.  Each tuple
            element of the list represents one response.  Each byte string
            element of the tuple is a full header line without delimiter, except
            for the last element which gives the full response body.
        """
        for response in expected:
            expectedHeaders, expectedContent = response[:-1], response[-1]
            # Intentionally avoid mutating the inputs here.
            expectedStatus = expectedHeaders[0]
            expectedHeaders = expectedHeaders[1:]

            headers, rest = responses.split(b'\r\n\r\n', 1)
            headers = headers.splitlines()
            status = headers.pop(0)

            self.assertEqual(expectedStatus, status)
            self.assertEqual(set(headers), set(expectedHeaders))
            content = rest[:len(expectedContent)]
            responses = rest[len(expectedContent):]
            self.assertEqual(content, expectedContent)



class HTTP1_0TestCase(unittest.TestCase, ResponseTestMixin):
    requests = (
        b"GET / HTTP/1.0\r\n"
        b"\r\n"
        b"GET / HTTP/1.1\r\n"
        b"Accept: text/html\r\n"
        b"\r\n")

    expected_response = [
        (b"HTTP/1.0 200 OK",
         b"Request: /",
         b"Command: GET",
         b"Version: HTTP/1.0",
         b"Content-Length: 13",
         b"'''\nNone\n'''\n")]

    def test_buffer(self):
        """
        Send requests over a channel and check responses match what is expected.
        """
        b = StringTransport()
        a = http.HTTPChannel()
        a.requestFactory = DummyHTTPHandler
        a.makeConnection(b)
        # one byte at a time, to stress it.
        for byte in iterbytes(self.requests):
            a.dataReceived(byte)
        a.connectionLost(IOError("all one"))
        value = b.value()
        self.assertResponseEquals(value, self.expected_response)


    def test_requestBodyTimeout(self):
        """
        L{HTTPChannel} resets its timeout whenever data from a request body is
        delivered to it.
        """
        clock = Clock()
        transport = StringTransport()
        protocol = http.HTTPChannel()
        protocol.timeOut = 100
        protocol.callLater = clock.callLater
        protocol.makeConnection(transport)
        protocol.dataReceived(b'POST / HTTP/1.0\r\nContent-Length: 2\r\n\r\n')
        clock.advance(99)
        self.assertFalse(transport.disconnecting)
        protocol.dataReceived(b'x')
        clock.advance(99)
        self.assertFalse(transport.disconnecting)
        protocol.dataReceived(b'x')
        self.assertEqual(len(protocol.requests), 1)



class HTTP1_1TestCase(HTTP1_0TestCase):

    requests = (
        b"GET / HTTP/1.1\r\n"
        b"Accept: text/html\r\n"
        b"\r\n"
        b"POST / HTTP/1.1\r\n"
        b"Content-Length: 10\r\n"
        b"\r\n"
        b"0123456789POST / HTTP/1.1\r\n"
        b"Content-Length: 10\r\n"
        b"\r\n"
        b"0123456789HEAD / HTTP/1.1\r\n"
        b"\r\n")

    expected_response = [
        (b"HTTP/1.1 200 OK",
         b"Request: /",
         b"Command: GET",
         b"Version: HTTP/1.1",
         b"Content-Length: 13",
         b"'''\nNone\n'''\n"),
        (b"HTTP/1.1 200 OK",
         b"Request: /",
         b"Command: POST",
         b"Version: HTTP/1.1",
         b"Content-Length: 21",
         b"'''\n10\n0123456789'''\n"),
        (b"HTTP/1.1 200 OK",
         b"Request: /",
         b"Command: POST",
         b"Version: HTTP/1.1",
         b"Content-Length: 21",
         b"'''\n10\n0123456789'''\n"),
        (b"HTTP/1.1 200 OK",
         b"Request: /",
         b"Command: HEAD",
         b"Version: HTTP/1.1",
         b"Content-Length: 13",
         b"")]



class HTTP1_1_close_TestCase(HTTP1_0TestCase):

    requests = (
        b"GET / HTTP/1.1\r\n"
        b"Accept: text/html\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"GET / HTTP/1.0\r\n"
        b"\r\n")

    expected_response = [
        (b"HTTP/1.1 200 OK",
         b"Connection: close",
         b"Request: /",
         b"Command: GET",
         b"Version: HTTP/1.1",
         b"Content-Length: 13",
         b"'''\nNone\n'''\n")]



class HTTP0_9TestCase(HTTP1_0TestCase):

    requests = (
        b"GET /\r\n")

    expected_response = b"HTTP/1.1 400 Bad Request\r\n\r\n"


    def assertResponseEquals(self, response, expectedResponse):
        self.assertEqual(response, expectedResponse)


class HTTPLoopbackTestCase(unittest.TestCase):

    expectedHeaders = {b'request': b'/foo/bar',
                       b'command': b'GET',
                       b'version': b'HTTP/1.0',
                       b'content-length': b'21'}
    numHeaders = 0
    gotStatus = 0
    gotResponse = 0
    gotEndHeaders = 0

    def _handleStatus(self, version, status, message):
        self.gotStatus = 1
        self.assertEqual(version, b"HTTP/1.0")
        self.assertEqual(status, b"200")

    def _handleResponse(self, data):
        self.gotResponse = 1
        self.assertEqual(data, b"'''\n10\n0123456789'''\n")

    def _handleHeader(self, key, value):
        self.numHeaders = self.numHeaders + 1
        self.assertEqual(self.expectedHeaders[key.lower()], value)

    def _handleEndHeaders(self):
        self.gotEndHeaders = 1
        self.assertEqual(self.numHeaders, 4)

    def testLoopback(self):
        server = http.HTTPChannel()
        server.requestFactory = DummyHTTPHandler
        client = LoopbackHTTPClient()
        client.handleResponse = self._handleResponse
        client.handleHeader = self._handleHeader
        client.handleEndHeaders = self._handleEndHeaders
        client.handleStatus = self._handleStatus
        d = loopback.loopbackAsync(server, client)
        d.addCallback(self._cbTestLoopback)
        return d

    def _cbTestLoopback(self, ignored):
        if not (self.gotStatus and self.gotResponse and self.gotEndHeaders):
            raise RuntimeError(
                "didn't got all callbacks %s"
                % [self.gotStatus, self.gotResponse, self.gotEndHeaders])
        del self.gotEndHeaders
        del self.gotResponse
        del self.gotStatus
        del self.numHeaders



def _prequest(**headers):
    """
    Make a request with the given request headers for the persistence tests.
    """
    request = http.Request(DummyChannel(), None)
    for headerName, v in headers.items():
        request.requestHeaders.setRawHeaders(networkString(headerName), v)
    return request



class PersistenceTestCase(unittest.TestCase):
    """
    Tests for persistent HTTP connections.
    """
    def setUp(self):
        self.channel = http.HTTPChannel()
        self.request = _prequest()


    def test_http09(self):
        """
        After being used for an I{HTTP/0.9} request, the L{HTTPChannel} is not
        persistent.
        """
        persist = self.channel.checkPersistence(self.request, b"HTTP/0.9")
        self.assertFalse(persist)
        self.assertEqual(
            [], list(self.request.responseHeaders.getAllRawHeaders()))


    def test_http10(self):
        """
        After being used for an I{HTTP/1.0} request, the L{HTTPChannel} is not
        persistent.
        """
        persist = self.channel.checkPersistence(self.request, b"HTTP/1.0")
        self.assertFalse(persist)
        self.assertEqual(
            [], list(self.request.responseHeaders.getAllRawHeaders()))


    def test_http11(self):
        """
        After being used for an I{HTTP/1.1} request, the L{HTTPChannel} is
        persistent.
        """
        persist = self.channel.checkPersistence(self.request, b"HTTP/1.1")
        self.assertTrue(persist)
        self.assertEqual(
            [], list(self.request.responseHeaders.getAllRawHeaders()))


    def test_http11Close(self):
        """
        After being used for an I{HTTP/1.1} request with a I{Connection: Close}
        header, the L{HTTPChannel} is not persistent.
        """
        request = _prequest(connection=[b"close"])
        persist = self.channel.checkPersistence(request, b"HTTP/1.1")
        self.assertFalse(persist)
        self.assertEqual(
            [(b"Connection", [b"close"])],
            list(request.responseHeaders.getAllRawHeaders()))



class IdentityTransferEncodingTests(TestCase):
    """
    Tests for L{_IdentityTransferDecoder}.
    """
    def setUp(self):
        """
        Create an L{_IdentityTransferDecoder} with callbacks hooked up so that
        calls to them can be inspected.
        """
        self.data = []
        self.finish = []
        self.contentLength = 10
        self.decoder = _IdentityTransferDecoder(
            self.contentLength, self.data.append, self.finish.append)


    def test_exactAmountReceived(self):
        """
        If L{_IdentityTransferDecoder.dataReceived} is called with a byte string
        with length equal to the content length passed to
        L{_IdentityTransferDecoder}'s initializer, the data callback is invoked
        with that string and the finish callback is invoked with a zero-length
        string.
        """
        self.decoder.dataReceived(b'x' * self.contentLength)
        self.assertEqual(self.data, [b'x' * self.contentLength])
        self.assertEqual(self.finish, [b''])


    def test_shortStrings(self):
        """
        If L{_IdentityTransferDecoder.dataReceived} is called multiple times
        with byte strings which, when concatenated, are as long as the content
        length provided, the data callback is invoked with each string and the
        finish callback is invoked only after the second call.
        """
        self.decoder.dataReceived(b'x')
        self.assertEqual(self.data, [b'x'])
        self.assertEqual(self.finish, [])
        self.decoder.dataReceived(b'y' * (self.contentLength - 1))
        self.assertEqual(self.data, [b'x', b'y' * (self.contentLength - 1)])
        self.assertEqual(self.finish, [b''])


    def test_longString(self):
        """
        If L{_IdentityTransferDecoder.dataReceived} is called with a byte string
        with length greater than the provided content length, only the prefix
        of that string up to the content length is passed to the data callback
        and the remainder is passed to the finish callback.
        """
        self.decoder.dataReceived(b'x' * self.contentLength + b'y')
        self.assertEqual(self.data, [b'x' * self.contentLength])
        self.assertEqual(self.finish, [b'y'])


    def test_rejectDataAfterFinished(self):
        """
        If data is passed to L{_IdentityTransferDecoder.dataReceived} after the
        finish callback has been invoked, C{RuntimeError} is raised.
        """
        failures = []
        def finish(bytes):
            try:
                decoder.dataReceived(b'foo')
            except:
                failures.append(Failure())
        decoder = _IdentityTransferDecoder(5, self.data.append, finish)
        decoder.dataReceived(b'x' * 4)
        self.assertEqual(failures, [])
        decoder.dataReceived(b'y')
        failures[0].trap(RuntimeError)
        self.assertEqual(
            str(failures[0].value),
            "_IdentityTransferDecoder cannot decode data after finishing")


    def test_unknownContentLength(self):
        """
        If L{_IdentityTransferDecoder} is constructed with C{None} for the
        content length, it passes all data delivered to it through to the data
        callback.
        """
        data = []
        finish = []
        decoder = _IdentityTransferDecoder(None, data.append, finish.append)
        decoder.dataReceived(b'x')
        self.assertEqual(data, [b'x'])
        decoder.dataReceived(b'y')
        self.assertEqual(data, [b'x', b'y'])
        self.assertEqual(finish, [])


    def _verifyCallbacksUnreferenced(self, decoder):
        """
        Check the decoder's data and finish callbacks and make sure they are
        None in order to help avoid references cycles.
        """
        self.assertIdentical(decoder.dataCallback, None)
        self.assertIdentical(decoder.finishCallback, None)


    def test_earlyConnectionLose(self):
        """
        L{_IdentityTransferDecoder.noMoreData} raises L{_DataLoss} if it is
        called and the content length is known but not enough bytes have been
        delivered.
        """
        self.decoder.dataReceived(b'x' * (self.contentLength - 1))
        self.assertRaises(_DataLoss, self.decoder.noMoreData)
        self._verifyCallbacksUnreferenced(self.decoder)


    def test_unknownContentLengthConnectionLose(self):
        """
        L{_IdentityTransferDecoder.noMoreData} calls the finish callback and
        raises L{PotentialDataLoss} if it is called and the content length is
        unknown.
        """
        body = []
        finished = []
        decoder = _IdentityTransferDecoder(None, body.append, finished.append)
        self.assertRaises(PotentialDataLoss, decoder.noMoreData)
        self.assertEqual(body, [])
        self.assertEqual(finished, [b''])
        self._verifyCallbacksUnreferenced(decoder)


    def test_finishedConnectionLose(self):
        """
        L{_IdentityTransferDecoder.noMoreData} does not raise any exception if
        it is called when the content length is known and that many bytes have
        been delivered.
        """
        self.decoder.dataReceived(b'x' * self.contentLength)
        self.decoder.noMoreData()
        self._verifyCallbacksUnreferenced(self.decoder)



class ChunkedTransferEncodingTests(unittest.TestCase):
    """
    Tests for L{_ChunkedTransferDecoder}, which turns a byte stream encoded
    using HTTP I{chunked} C{Transfer-Encoding} back into the original byte
    stream.
    """
    def test_decoding(self):
        """
        L{_ChunkedTransferDecoder.dataReceived} decodes chunked-encoded data
        and passes the result to the specified callback.
        """
        L = []
        p = http._ChunkedTransferDecoder(L.append, None)
        p.dataReceived(b'3\r\nabc\r\n5\r\n12345\r\n')
        p.dataReceived(b'a\r\n0123456789\r\n')
        self.assertEqual(L, [b'abc', b'12345', b'0123456789'])


    def test_short(self):
        """
        L{_ChunkedTransferDecoder.dataReceived} decodes chunks broken up and
        delivered in multiple calls.
        """
        L = []
        finished = []
        p = http._ChunkedTransferDecoder(L.append, finished.append)
        for s in iterbytes(b'3\r\nabc\r\n5\r\n12345\r\n0\r\n\r\n'):
            p.dataReceived(s)
        self.assertEqual(L, [b'a', b'b', b'c', b'1', b'2', b'3', b'4', b'5'])
        self.assertEqual(finished, [b''])


    def test_newlines(self):
        """
        L{_ChunkedTransferDecoder.dataReceived} doesn't treat CR LF pairs
        embedded in chunk bodies specially.
        """
        L = []
        p = http._ChunkedTransferDecoder(L.append, None)
        p.dataReceived(b'2\r\n\r\n\r\n')
        self.assertEqual(L, [b'\r\n'])


    def test_extensions(self):
        """
        L{_ChunkedTransferDecoder.dataReceived} disregards chunk-extension
        fields.
        """
        L = []
        p = http._ChunkedTransferDecoder(L.append, None)
        p.dataReceived(b'3; x-foo=bar\r\nabc\r\n')
        self.assertEqual(L, [b'abc'])


    def test_finish(self):
        """
        L{_ChunkedTransferDecoder.dataReceived} interprets a zero-length
        chunk as the end of the chunked data stream and calls the completion
        callback.
        """
        finished = []
        p = http._ChunkedTransferDecoder(None, finished.append)
        p.dataReceived(b'0\r\n\r\n')
        self.assertEqual(finished, [b''])


    def test_extra(self):
        """
        L{_ChunkedTransferDecoder.dataReceived} passes any bytes which come
        after the terminating zero-length chunk to the completion callback.
        """
        finished = []
        p = http._ChunkedTransferDecoder(None, finished.append)
        p.dataReceived(b'0\r\n\r\nhello')
        self.assertEqual(finished, [b'hello'])


    def test_afterFinished(self):
        """
        L{_ChunkedTransferDecoder.dataReceived} raises C{RuntimeError} if it
        is called after it has seen the last chunk.
        """
        p = http._ChunkedTransferDecoder(None, lambda bytes: None)
        p.dataReceived(b'0\r\n\r\n')
        self.assertRaises(RuntimeError, p.dataReceived, b'hello')


    def test_earlyConnectionLose(self):
        """
        L{_ChunkedTransferDecoder.noMoreData} raises L{_DataLoss} if it is
        called and the end of the last trailer has not yet been received.
        """
        parser = http._ChunkedTransferDecoder(None, lambda bytes: None)
        parser.dataReceived(b'0\r\n\r')
        exc = self.assertRaises(_DataLoss, parser.noMoreData)
        self.assertEqual(
            str(exc),
            "Chunked decoder in 'TRAILER' state, still expecting more data "
            "to get to 'FINISHED' state.")


    def test_finishedConnectionLose(self):
        """
        L{_ChunkedTransferDecoder.noMoreData} does not raise any exception if
        it is called after the terminal zero length chunk is received.
        """
        parser = http._ChunkedTransferDecoder(None, lambda bytes: None)
        parser.dataReceived(b'0\r\n\r\n')
        parser.noMoreData()


    def test_reentrantFinishedNoMoreData(self):
        """
        L{_ChunkedTransferDecoder.noMoreData} can be called from the finished
        callback without raising an exception.
        """
        errors = []
        successes = []
        def finished(extra):
            try:
                parser.noMoreData()
            except:
                errors.append(Failure())
            else:
                successes.append(True)
        parser = http._ChunkedTransferDecoder(None, finished)
        parser.dataReceived(b'0\r\n\r\n')
        self.assertEqual(errors, [])
        self.assertEqual(successes, [True])



class ChunkingTestCase(unittest.TestCase):

    strings = [b"abcv", b"", b"fdfsd423", b"Ffasfas\r\n",
               b"523523\n\rfsdf", b"4234"]

    def testChunks(self):
        for s in self.strings:
            chunked = b''.join(http.toChunk(s))
            self.assertEqual((s, b''), http.fromChunk(chunked))
        self.assertRaises(ValueError, http.fromChunk, b'-5\r\nmalformed!\r\n')

    def testConcatenatedChunks(self):
        chunked = b''.join([b''.join(http.toChunk(t)) for t in self.strings])
        result = []
        buffer = b""
        for c in iterbytes(chunked):
            buffer = buffer + c
            try:
                data, buffer = http.fromChunk(buffer)
                result.append(data)
            except ValueError:
                pass
        self.assertEqual(result, self.strings)



class ParsingTestCase(unittest.TestCase):
    """
    Tests for protocol parsing in L{HTTPChannel}.
    """
    def setUp(self):
        self.didRequest = False


    def runRequest(self, httpRequest, requestClass, success=1):
        httpRequest = httpRequest.replace(b"\n", b"\r\n")
        b = StringTransport()
        a = http.HTTPChannel()
        a.requestFactory = requestClass
        a.makeConnection(b)
        # one byte at a time, to stress it.
        for byte in iterbytes(httpRequest):
            if a.transport.disconnecting:
                break
            a.dataReceived(byte)
        a.connectionLost(IOError("all done"))
        if success:
            self.assertTrue(self.didRequest)
        else:
            self.assertFalse(self.didRequest)
        return a


    def test_basicAuth(self):
        """
        L{HTTPChannel} provides username and password information supplied in
        an I{Authorization} header to the L{Request} which makes it available
        via its C{getUser} and C{getPassword} methods.
        """
        requests = []
        class Request(http.Request):
            def process(self):
                self.credentials = (self.getUser(), self.getPassword())
                requests.append(self)

        for u, p in [(b"foo", b"bar"), (b"hello", b"there:z")]:
            s = base64.encodestring(b":".join((u, p))).strip()
            f = b"GET / HTTP/1.0\nAuthorization: Basic " + s + b"\n\n"
            self.runRequest(f, Request, 0)
            req = requests.pop()
            self.assertEqual((u, p), req.credentials)


    def test_headers(self):
        """
        Headers received by L{HTTPChannel} in a request are made available to
        the L{Request}.
        """
        processed = []
        class MyRequest(http.Request):
            def process(self):
                processed.append(self)
                self.finish()

        requestLines = [
            b"GET / HTTP/1.0",
            b"Foo: bar",
            b"baz: Quux",
            b"baz: quux",
            b"",
            b""]

        self.runRequest(b'\n'.join(requestLines), MyRequest, 0)
        [request] = processed
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b'foo'), [b'bar'])
        self.assertEqual(
            request.requestHeaders.getRawHeaders(b'bAz'), [b'Quux', b'quux'])


    def test_tooManyHeaders(self):
        """
        L{HTTPChannel} enforces a limit of C{HTTPChannel.maxHeaders} on the
        number of headers received per request.
        """
        processed = []
        class MyRequest(http.Request):
            def process(self):
                processed.append(self)

        requestLines = [b"GET / HTTP/1.0"]
        for i in range(http.HTTPChannel.maxHeaders + 2):
            requestLines.append(networkString("%s: foo" % (i,)))
        requestLines.extend([b"", b""])

        channel = self.runRequest(b"\n".join(requestLines), MyRequest, 0)
        self.assertEqual(processed, [])
        self.assertEqual(
            channel.transport.value(),
            b"HTTP/1.1 400 Bad Request\r\n\r\n")


    def test_invalidHeaders(self):
        """
        If a Content-Length header with a non-integer value is received, a 400
        (Bad Request) response is sent to the client and the connection is
        closed.
        """
        requestLines = [b"GET / HTTP/1.0", b"Content-Length: x", b"", b""]
        channel = self.runRequest(b"\n".join(requestLines), http.Request, 0)
        self.assertEqual(
            channel.transport.value(),
            b"HTTP/1.1 400 Bad Request\r\n\r\n")
        self.assertTrue(channel.transport.disconnecting)


    def test_headerLimitPerRequest(self):
        """
        L{HTTPChannel} enforces the limit of C{HTTPChannel.maxHeaders} per
        request so that headers received in an earlier request do not count
        towards the limit when processing a later request.
        """
        processed = []
        class MyRequest(http.Request):
            def process(self):
                processed.append(self)
                self.finish()

        self.patch(http.HTTPChannel, 'maxHeaders', 1)
        requestLines = [
            b"GET / HTTP/1.1",
            b"Foo: bar",
            b"",
            b"",
            b"GET / HTTP/1.1",
            b"Bar: baz",
            b"",
            b""]

        channel = self.runRequest(b"\n".join(requestLines), MyRequest, 0)
        [first, second] = processed
        self.assertEqual(first.getHeader(b'foo'), b'bar')
        self.assertEqual(second.getHeader(b'bar'), b'baz')
        self.assertEqual(
            channel.transport.value(),
            b'HTTP/1.1 200 OK\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'\r\n'
            b'0\r\n'
            b'\r\n'
            b'HTTP/1.1 200 OK\r\n'
            b'Transfer-Encoding: chunked\r\n'
            b'\r\n'
            b'0\r\n'
            b'\r\n')


    def testCookies(self):
        """
        Test cookies parsing and reading.
        """
        httpRequest = b'''\
GET / HTTP/1.0
Cookie: rabbit="eat carrot"; ninja=secret; spam="hey 1=1!"

'''
        cookies = {}
        testcase = self
        class MyRequest(http.Request):
            def process(self):
                for name in [b'rabbit', b'ninja', b'spam']:
                    cookies[name] = self.getCookie(name)
                testcase.didRequest = True
                self.finish()

        self.runRequest(httpRequest, MyRequest)

        self.assertEqual(
            cookies, {
                b'rabbit': b'"eat carrot"',
                b'ninja': b'secret',
                b'spam': b'"hey 1=1!"'})


    def testGET(self):
        httpRequest = b'''\
GET /?key=value&multiple=two+words&multiple=more%20words&empty= HTTP/1.0

'''
        method = []
        args = []
        testcase = self
        class MyRequest(http.Request):
            def process(self):
                method.append(self.method)
                args.extend([
                        self.args[b"key"],
                        self.args[b"empty"],
                        self.args[b"multiple"]])
                testcase.didRequest = True
                self.finish()

        self.runRequest(httpRequest, MyRequest)
        self.assertEqual(method, [b"GET"])
        self.assertEqual(
            args, [[b"value"], [b""], [b"two words", b"more words"]])


    def test_extraQuestionMark(self):
        """
        While only a single '?' is allowed in an URL, several other servers
        allow several and pass all after the first through as part of the
        query arguments.  Test that we emulate this behavior.
        """
        httpRequest = b'GET /foo?bar=?&baz=quux HTTP/1.0\n\n'

        method = []
        path = []
        args = []
        testcase = self
        class MyRequest(http.Request):
            def process(self):
                method.append(self.method)
                path.append(self.path)
                args.extend([self.args[b'bar'], self.args[b'baz']])
                testcase.didRequest = True
                self.finish()

        self.runRequest(httpRequest, MyRequest)
        self.assertEqual(method, [b'GET'])
        self.assertEqual(path, [b'/foo'])
        self.assertEqual(args, [[b'?'], [b'quux']])


    def test_formPOSTRequest(self):
        """
        The request body of a I{POST} request with a I{Content-Type} header
        of I{application/x-www-form-urlencoded} is parsed according to that
        content type and made available in the C{args} attribute of the
        request object.  The original bytes of the request may still be read
        from the C{content} attribute.
        """
        query = 'key=value&multiple=two+words&multiple=more%20words&empty='
        httpRequest = networkString('''\
POST / HTTP/1.0
Content-Length: %d
Content-Type: application/x-www-form-urlencoded

%s''' % (len(query), query))

        method = []
        args = []
        content = []
        testcase = self
        class MyRequest(http.Request):
            def process(self):
                method.append(self.method)
                args.extend([
                        self.args[b'key'], self.args[b'empty'],
                        self.args[b'multiple']])
                content.append(self.content.read())
                testcase.didRequest = True
                self.finish()

        self.runRequest(httpRequest, MyRequest)
        self.assertEqual(method, [b"POST"])
        self.assertEqual(
            args, [[b"value"], [b""], [b"two words", b"more words"]])
        # Reading from the content file-like must produce the entire request
        # body.
        self.assertEqual(content, [networkString(query)])


    def testMissingContentDisposition(self):
        req = b'''\
POST / HTTP/1.0
Content-Type: multipart/form-data; boundary=AaB03x
Content-Length: 103

--AaB03x
Content-Type: text/plain
Content-Transfer-Encoding: quoted-printable

abasdfg
--AaB03x--
'''
        self.runRequest(req, http.Request, success=False)
    if _PY3:
        testMissingContentDisposition.skip = (
            "Cannot parse multipart/form-data on Python 3.  "
            "See http://bugs.python.org/issue12411 and #5511.")


    def test_chunkedEncoding(self):
        """
        If a request uses the I{chunked} transfer encoding, the request body is
        decoded accordingly before it is made available on the request.
        """
        httpRequest = b'''\
GET / HTTP/1.0
Content-Type: text/plain
Transfer-Encoding: chunked

6
Hello,
14
 spam,eggs spam spam
0

'''
        path = []
        method = []
        content = []
        decoder = []
        testcase = self
        class MyRequest(http.Request):
            def process(self):
                content.append(self.content.fileno())
                content.append(self.content.read())
                method.append(self.method)
                path.append(self.path)
                decoder.append(self.channel._transferDecoder)
                testcase.didRequest = True
                self.finish()

        self.runRequest(httpRequest, MyRequest)
        # The tempfile API used to create content returns an
        # instance of a different type depending on what platform
        # we're running on.  The point here is to verify that the
        # request body is in a file that's on the filesystem.
        # Having a fileno method that returns an int is a somewhat
        # close approximation of this. -exarkun
        self.assertIsInstance(content[0], int)
        self.assertEqual(content[1], b'Hello, spam,eggs spam spam')
        self.assertEqual(method, [b'GET'])
        self.assertEqual(path, [b'/'])
        self.assertEqual(decoder, [None])


    def test_malformedChunkedEncoding(self):
        """
        If a request uses the I{chunked} transfer encoding, but provides an
        invalid chunk length value, the request fails with a 400 error.
        """
        # See test_chunkedEncoding for the correct form of this request.
        httpRequest = b'''\
GET / HTTP/1.1
Content-Type: text/plain
Transfer-Encoding: chunked

MALFORMED_LINE_THIS_SHOULD_BE_'6'
Hello,
14
 spam,eggs spam spam
0

'''
        didRequest = []

        class MyRequest(http.Request):

            def process(self):
                # This request should fail, so this should never be called.
                didRequest.append(True)

        channel = self.runRequest(httpRequest, MyRequest, success=False)
        self.assertFalse(didRequest, "Request.process called")
        self.assertEqual(
            channel.transport.value(),
            b"HTTP/1.1 400 Bad Request\r\n\r\n")
        self.assertTrue(channel.transport.disconnecting)



class QueryArgumentsTestCase(unittest.TestCase):
    def testParseqs(self):
        self.assertEqual(
            cgi.parse_qs(b"a=b&d=c;+=f"),
            http.parse_qs(b"a=b&d=c;+=f"))
        self.assertRaises(
            ValueError, http.parse_qs, b"blah", strict_parsing=True)
        self.assertEqual(
            cgi.parse_qs(b"a=&b=c", keep_blank_values=1),
            http.parse_qs(b"a=&b=c", keep_blank_values=1))
        self.assertEqual(
            cgi.parse_qs(b"a=&b=c"),
            http.parse_qs(b"a=&b=c"))


    def test_urlparse(self):
        """
        For a given URL, L{http.urlparse} should behave the same as L{urlparse},
        except it should always return C{bytes}, never text.
        """
        def urls():
            for scheme in (b'http', b'https'):
                for host in (b'example.com',):
                    for port in (None, 100):
                        for path in (b'', b'path'):
                            if port is not None:
                                host = host + b':' + networkString(str(port))
                                yield urlunsplit((scheme, host, path, b'', b''))


        def assertSameParsing(url, decode):
            """
            Verify that C{url} is parsed into the same objects by both
            L{http.urlparse} and L{urlparse}.
            """
            urlToStandardImplementation = url
            if decode:
                urlToStandardImplementation = url.decode('ascii')

            # stdlib urlparse will give back whatever type we give it.  To be
            # able to compare the values meaningfully, if it gives back unicode,
            # convert all the values to bytes.
            standardResult = urlparse(urlToStandardImplementation)
            if isinstance(standardResult.scheme, unicode):
                # The choice of encoding is basically irrelevant.  The values
                # are all in ASCII.  UTF-8 is, of course, the correct choice.
                expected = (standardResult.scheme.encode('utf-8'),
                            standardResult.netloc.encode('utf-8'),
                            standardResult.path.encode('utf-8'),
                            standardResult.params.encode('utf-8'),
                            standardResult.query.encode('utf-8'),
                            standardResult.fragment.encode('utf-8'))
            else:
                expected = (standardResult.scheme,
                            standardResult.netloc,
                            standardResult.path,
                            standardResult.params,
                            standardResult.query,
                            standardResult.fragment)

            scheme, netloc, path, params, query, fragment = http.urlparse(url)
            self.assertEqual(
                (scheme, netloc, path, params, query, fragment), expected)
            self.assertIsInstance(scheme, bytes)
            self.assertIsInstance(netloc, bytes)
            self.assertIsInstance(path, bytes)
            self.assertIsInstance(params, bytes)
            self.assertIsInstance(query, bytes)
            self.assertIsInstance(fragment, bytes)

        # With caching, unicode then str
        clear_cache()
        for url in urls():
            assertSameParsing(url, True)
            assertSameParsing(url, False)

        # With caching, str then unicode
        clear_cache()
        for url in urls():
            assertSameParsing(url, False)
            assertSameParsing(url, True)

        # Without caching
        for url in urls():
            clear_cache()
            assertSameParsing(url, True)
            clear_cache()
            assertSameParsing(url, False)


    def test_urlparseRejectsUnicode(self):
        """
        L{http.urlparse} should reject unicode input early.
        """
        self.assertRaises(TypeError, http.urlparse, u'http://example.org/path')



class ClientDriver(http.HTTPClient):
    def handleStatus(self, version, status, message):
        self.version = version
        self.status = status
        self.message = message

class ClientStatusParsing(unittest.TestCase):
    def testBaseline(self):
        c = ClientDriver()
        c.lineReceived(b'HTTP/1.0 201 foo')
        self.assertEqual(c.version, b'HTTP/1.0')
        self.assertEqual(c.status, b'201')
        self.assertEqual(c.message, b'foo')

    def testNoMessage(self):
        c = ClientDriver()
        c.lineReceived(b'HTTP/1.0 201')
        self.assertEqual(c.version, b'HTTP/1.0')
        self.assertEqual(c.status, b'201')
        self.assertEqual(c.message, b'')

    def testNoMessage_trailingSpace(self):
        c = ClientDriver()
        c.lineReceived(b'HTTP/1.0 201 ')
        self.assertEqual(c.version, b'HTTP/1.0')
        self.assertEqual(c.status, b'201')
        self.assertEqual(c.message, b'')



class RequestTests(unittest.TestCase, ResponseTestMixin):
    """
    Tests for L{http.Request}
    """
    def _compatHeadersTest(self, oldName, newName):
        """
        Verify that each of two different attributes which are associated with
        the same state properly reflect changes made through the other.

        This is used to test that the C{headers}/C{responseHeaders} and
        C{received_headers}/C{requestHeaders} pairs interact properly.
        """
        req = http.Request(DummyChannel(), None)
        getattr(req, newName).setRawHeaders(b"test", [b"lemur"])
        self.assertEqual(getattr(req, oldName)[b"test"], b"lemur")
        setattr(req, oldName, {b"foo": b"bar"})
        self.assertEqual(
            list(getattr(req, newName).getAllRawHeaders()),
            [(b"Foo", [b"bar"])])
        setattr(req, newName, http_headers.Headers())
        self.assertEqual(getattr(req, oldName), {})


    def test_received_headers(self):
        """
        L{Request.received_headers} is a backwards compatible API which
        accesses and allows mutation of the state at L{Request.requestHeaders}.
        """
        self._compatHeadersTest('received_headers', 'requestHeaders')


    def test_headers(self):
        """
        L{Request.headers} is a backwards compatible API which accesses and
        allows mutation of the state at L{Request.responseHeaders}.
        """
        self._compatHeadersTest('headers', 'responseHeaders')


    def test_getHeader(self):
        """
        L{http.Request.getHeader} returns the value of the named request
        header.
        """
        req = http.Request(DummyChannel(), None)
        req.requestHeaders.setRawHeaders(b"test", [b"lemur"])
        self.assertEqual(req.getHeader(b"test"), b"lemur")


    def test_getHeaderReceivedMultiples(self):
        """
        When there are multiple values for a single request header,
        L{http.Request.getHeader} returns the last value.
        """
        req = http.Request(DummyChannel(), None)
        req.requestHeaders.setRawHeaders(b"test", [b"lemur", b"panda"])
        self.assertEqual(req.getHeader(b"test"), b"panda")


    def test_getHeaderNotFound(self):
        """
        L{http.Request.getHeader} returns C{None} when asked for the value of a
        request header which is not present.
        """
        req = http.Request(DummyChannel(), None)
        self.assertEqual(req.getHeader(b"test"), None)


    def test_getAllHeaders(self):
        """
        L{http.Request.getAllheaders} returns a C{dict} mapping all request
        header names to their corresponding values.
        """
        req = http.Request(DummyChannel(), None)
        req.requestHeaders.setRawHeaders(b"test", [b"lemur"])
        self.assertEqual(req.getAllHeaders(), {b"test": b"lemur"})


    def test_getAllHeadersNoHeaders(self):
        """
        L{http.Request.getAllHeaders} returns an empty C{dict} if there are no
        request headers.
        """
        req = http.Request(DummyChannel(), None)
        self.assertEqual(req.getAllHeaders(), {})


    def test_getAllHeadersMultipleHeaders(self):
        """
        When there are multiple values for a single request header,
        L{http.Request.getAllHeaders} returns only the last value.
        """
        req = http.Request(DummyChannel(), None)
        req.requestHeaders.setRawHeaders(b"test", [b"lemur", b"panda"])
        self.assertEqual(req.getAllHeaders(), {b"test": b"panda"})


    def test_setResponseCode(self):
        """
        L{http.Request.setResponseCode} takes a status code and causes it to be
        used as the response status.
        """
        channel = DummyChannel()
        req = http.Request(channel, None)
        req.setResponseCode(201)
        req.write(b'')
        self.assertEqual(
            channel.transport.written.getvalue().splitlines()[0],
            b"(no clientproto yet) 201 Created")


    def test_setResponseCodeAndMessage(self):
        """
        L{http.Request.setResponseCode} takes a status code and a message and
        causes them to be used as the response status.
        """
        channel = DummyChannel()
        req = http.Request(channel, None)
        req.setResponseCode(202, "happily accepted")
        req.write(b'')
        self.assertEqual(
            channel.transport.written.getvalue().splitlines()[0],
            b'(no clientproto yet) 202 happily accepted')


    def test_setResponseCodeAcceptsIntegers(self):
        """
        L{http.Request.setResponseCode} accepts C{int} for the code parameter
        and raises L{TypeError} if passed anything else.
        """
        req = http.Request(DummyChannel(), None)
        req.setResponseCode(1)
        self.assertRaises(TypeError, req.setResponseCode, "1")


    def test_setResponseCodeAcceptsLongIntegers(self):
        """
        L{http.Request.setResponseCode} accepts C{long} for the code
        parameter.
        """
        req = http.Request(DummyChannel(), None)
        req.setResponseCode(long(1))
    if _PY3:
        test_setResponseCodeAcceptsLongIntegers.skip = (
            "Python 3 has no separate long integer type.")


    def test_setHost(self):
        """
        L{http.Request.setHost} sets the value of the host request header.
        The port should not be added because it is the default.
        """
        req = http.Request(DummyChannel(), None)
        req.setHost(b"example.com", 80)
        self.assertEqual(
            req.requestHeaders.getRawHeaders(b"host"), [b"example.com"])


    def test_setHostSSL(self):
        """
        L{http.Request.setHost} sets the value of the host request header.
        The port should not be added because it is the default.
        """
        d = DummyChannel()
        d.transport = DummyChannel.SSL()
        req = http.Request(d, None)
        req.setHost(b"example.com", 443)
        self.assertEqual(
            req.requestHeaders.getRawHeaders(b"host"), [b"example.com"])


    def test_setHostNonDefaultPort(self):
        """
        L{http.Request.setHost} sets the value of the host request header.
        The port should be added because it is not the default.
        """
        req = http.Request(DummyChannel(), None)
        req.setHost(b"example.com", 81)
        self.assertEqual(
            req.requestHeaders.getRawHeaders(b"host"), [b"example.com:81"])


    def test_setHostSSLNonDefaultPort(self):
        """
        L{http.Request.setHost} sets the value of the host request header.
        The port should be added because it is not the default.
        """
        d = DummyChannel()
        d.transport = DummyChannel.SSL()
        req = http.Request(d, None)
        req.setHost(b"example.com", 81)
        self.assertEqual(
            req.requestHeaders.getRawHeaders(b"host"), [b"example.com:81"])


    def test_setHeader(self):
        """
        L{http.Request.setHeader} sets the value of the given response header.
        """
        req = http.Request(DummyChannel(), None)
        req.setHeader(b"test", b"lemur")
        self.assertEqual(req.responseHeaders.getRawHeaders(b"test"), [b"lemur"])


    def test_firstWrite(self):
        """
        For an HTTP 1.0 request, L{http.Request.write} sends an HTTP 1.0
        Response-Line and whatever response headers are set.
        """
        req = http.Request(DummyChannel(), None)
        trans = StringTransport()

        req.transport = trans

        req.setResponseCode(200)
        req.clientproto = b"HTTP/1.0"
        req.responseHeaders.setRawHeaders(b"test", [b"lemur"])
        req.write(b'Hello')

        self.assertResponseEquals(
            trans.value(),
            [(b"HTTP/1.0 200 OK",
              b"Test: lemur",
              b"Hello")])


    def test_nonByteHeaderValue(self):
        """
        L{http.Request.write} casts non-bytes header value to bytes
        transparently.
        """
        req = http.Request(DummyChannel(), None)
        trans = StringTransport()

        req.transport = trans

        req.setResponseCode(200)
        req.clientproto = b"HTTP/1.0"
        req.responseHeaders.setRawHeaders(b"test", [10])
        req.write(b'Hello')

        self.assertResponseEquals(
            trans.value(),
            [(b"HTTP/1.0 200 OK",
              b"Test: 10",
              b"Hello")])

        warnings = self.flushWarnings(
            offendingFunctions=[self.test_nonByteHeaderValue])
        self.assertEqual(1, len(warnings))
        self.assertEqual(warnings[0]['category'], DeprecationWarning)
        self.assertEqual(
            warnings[0]['message'],
            "Passing non-bytes header values is deprecated since "
            "Twisted 12.3. Pass only bytes instead.")


    def test_firstWriteHTTP11Chunked(self):
        """
        For an HTTP 1.1 request, L{http.Request.write} sends an HTTP 1.1
        Response-Line, whatever response headers are set, and uses chunked
        encoding for the response body.
        """
        req = http.Request(DummyChannel(), None)
        trans = StringTransport()

        req.transport = trans

        req.setResponseCode(200)
        req.clientproto = b"HTTP/1.1"
        req.responseHeaders.setRawHeaders(b"test", [b"lemur"])
        req.write(b'Hello')
        req.write(b'World!')

        self.assertResponseEquals(
            trans.value(),
            [(b"HTTP/1.1 200 OK",
              b"Test: lemur",
              b"Transfer-Encoding: chunked",
              b"5\r\nHello\r\n6\r\nWorld!\r\n")])


    def test_firstWriteLastModified(self):
        """
        For an HTTP 1.0 request for a resource with a known last modified time,
        L{http.Request.write} sends an HTTP Response-Line, whatever response
        headers are set, and a last-modified header with that time.
        """
        req = http.Request(DummyChannel(), None)
        trans = StringTransport()

        req.transport = trans

        req.setResponseCode(200)
        req.clientproto = b"HTTP/1.0"
        req.lastModified = 0
        req.responseHeaders.setRawHeaders(b"test", [b"lemur"])
        req.write(b'Hello')

        self.assertResponseEquals(
            trans.value(),
            [(b"HTTP/1.0 200 OK",
              b"Test: lemur",
              b"Last-Modified: Thu, 01 Jan 1970 00:00:00 GMT",
              b"Hello")])


    def test_parseCookies(self):
        """
        L{http.Request.parseCookies} extracts cookies from C{requestHeaders}
        and adds them to C{received_cookies}.
        """
        req = http.Request(DummyChannel(), None)
        req.requestHeaders.setRawHeaders(
            b"cookie", [b'test="lemur"; test2="panda"'])
        req.parseCookies()
        self.assertEqual(
            req.received_cookies, {b"test": b'"lemur"', b"test2": b'"panda"'})


    def test_parseCookiesMultipleHeaders(self):
        """
        L{http.Request.parseCookies} can extract cookies from multiple Cookie
        headers.
        """
        req = http.Request(DummyChannel(), None)
        req.requestHeaders.setRawHeaders(
            b"cookie", [b'test="lemur"', b'test2="panda"'])
        req.parseCookies()
        self.assertEqual(
            req.received_cookies, {b"test": b'"lemur"', b"test2": b'"panda"'})


    def test_connectionLost(self):
        """
        L{http.Request.connectionLost} closes L{Request.content} and drops the
        reference to the L{HTTPChannel} to assist with garbage collection.
        """
        req = http.Request(DummyChannel(), None)

        # Cause Request.content to be created at all.
        req.gotLength(10)

        # Grab a reference to content in case the Request drops it later on.
        content = req.content

        # Put some bytes into it
        req.handleContentChunk(b"hello")

        # Then something goes wrong and content should get closed.
        req.connectionLost(Failure(ConnectionLost("Finished")))
        self.assertTrue(content.closed)
        self.assertIdentical(req.channel, None)


    def test_registerProducerTwiceFails(self):
        """
        Calling L{Request.registerProducer} when a producer is already
        registered raises ValueError.
        """
        req = http.Request(DummyChannel(), None)
        req.registerProducer(DummyProducer(), True)
        self.assertRaises(
            ValueError, req.registerProducer, DummyProducer(), True)


    def test_registerProducerWhenQueuedPausesPushProducer(self):
        """
        Calling L{Request.registerProducer} with an IPushProducer when the
        request is queued pauses the producer.
        """
        req = http.Request(DummyChannel(), True)
        producer = DummyProducer()
        req.registerProducer(producer, True)
        self.assertEqual(['pause'], producer.events)


    def test_registerProducerWhenQueuedDoesntPausePullProducer(self):
        """
        Calling L{Request.registerProducer} with an IPullProducer when the
        request is queued does not pause the producer, because it doesn't make
        sense to pause a pull producer.
        """
        req = http.Request(DummyChannel(), True)
        producer = DummyProducer()
        req.registerProducer(producer, False)
        self.assertEqual([], producer.events)


    def test_registerProducerWhenQueuedDoesntRegisterPushProducer(self):
        """
        Calling L{Request.registerProducer} with an IPushProducer when the
        request is queued does not register the producer on the request's
        transport.
        """
        self.assertIdentical(
            None, getattr(http.StringTransport, 'registerProducer', None),
            "StringTransport cannot implement registerProducer for this test "
            "to be valid.")
        req = http.Request(DummyChannel(), True)
        producer = DummyProducer()
        req.registerProducer(producer, True)
        # This is a roundabout assertion: http.StringTransport doesn't
        # implement registerProducer, so Request.registerProducer can't have
        # tried to call registerProducer on the transport.
        self.assertIsInstance(req.transport, http.StringTransport)


    def test_registerProducerWhenQueuedDoesntRegisterPullProducer(self):
        """
        Calling L{Request.registerProducer} with an IPullProducer when the
        request is queued does not register the producer on the request's
        transport.
        """
        self.assertIdentical(
            None, getattr(http.StringTransport, 'registerProducer', None),
            "StringTransport cannot implement registerProducer for this test "
            "to be valid.")
        req = http.Request(DummyChannel(), True)
        producer = DummyProducer()
        req.registerProducer(producer, False)
        # This is a roundabout assertion: http.StringTransport doesn't
        # implement registerProducer, so Request.registerProducer can't have
        # tried to call registerProducer on the transport.
        self.assertIsInstance(req.transport, http.StringTransport)


    def test_registerProducerWhenNotQueuedRegistersPushProducer(self):
        """
        Calling L{Request.registerProducer} with an IPushProducer when the
        request is not queued registers the producer as a push producer on the
        request's transport.
        """
        req = http.Request(DummyChannel(), False)
        producer = DummyProducer()
        req.registerProducer(producer, True)
        self.assertEqual([(producer, True)], req.transport.producers)


    def test_registerProducerWhenNotQueuedRegistersPullProducer(self):
        """
        Calling L{Request.registerProducer} with an IPullProducer when the
        request is not queued registers the producer as a pull producer on the
        request's transport.
        """
        req = http.Request(DummyChannel(), False)
        producer = DummyProducer()
        req.registerProducer(producer, False)
        self.assertEqual([(producer, False)], req.transport.producers)


    def test_connectionLostNotification(self):
        """
        L{Request.connectionLost} triggers all finish notification Deferreds
        and cleans up per-request state.
        """
        d = DummyChannel()
        request = http.Request(d, True)
        finished = request.notifyFinish()
        request.connectionLost(Failure(ConnectionLost("Connection done")))
        self.assertIdentical(request.channel, None)
        return self.assertFailure(finished, ConnectionLost)


    def test_finishNotification(self):
        """
        L{Request.finish} triggers all finish notification Deferreds.
        """
        request = http.Request(DummyChannel(), False)
        finished = request.notifyFinish()
        # Force the request to have a non-None content attribute.  This is
        # probably a bug in Request.
        request.gotLength(1)
        request.finish()
        return finished


    def test_writeAfterFinish(self):
        """
        Calling L{Request.write} after L{Request.finish} has been called results
        in a L{RuntimeError} being raised.
        """
        request = http.Request(DummyChannel(), False)
        finished = request.notifyFinish()
        # Force the request to have a non-None content attribute.  This is
        # probably a bug in Request.
        request.gotLength(1)
        request.write(b'foobar')
        request.finish()
        self.assertRaises(RuntimeError, request.write, b'foobar')
        return finished


    def test_finishAfterConnectionLost(self):
        """
        Calling L{Request.finish} after L{Request.connectionLost} has been
        called results in a L{RuntimeError} being raised.
        """
        channel = DummyChannel()
        req = http.Request(channel, False)
        req.connectionLost(Failure(ConnectionLost("The end.")))
        self.assertRaises(RuntimeError, req.finish)



class MultilineHeadersTestCase(unittest.TestCase):
    """
    Tests to exercise handling of multiline headers by L{HTTPClient}.  RFCs 1945
    (HTTP 1.0) and 2616 (HTTP 1.1) state that HTTP message header fields can
    span multiple lines if each extra line is preceded by at least one space or
    horizontal tab.
    """
    def setUp(self):
        """
        Initialize variables used to verify that the header-processing functions
        are getting called.
        """
        self.handleHeaderCalled = False
        self.handleEndHeadersCalled = False

    # Dictionary of sample complete HTTP header key/value pairs, including
    # multiline headers.
    expectedHeaders = {b'Content-Length': b'10',
                       b'X-Multiline' : b'line-0\tline-1',
                       b'X-Multiline2' : b'line-2 line-3'}

    def ourHandleHeader(self, key, val):
        """
        Dummy implementation of L{HTTPClient.handleHeader}.
        """
        self.handleHeaderCalled = True
        self.assertEqual(val, self.expectedHeaders[key])


    def ourHandleEndHeaders(self):
        """
        Dummy implementation of L{HTTPClient.handleEndHeaders}.
        """
        self.handleEndHeadersCalled = True


    def test_extractHeader(self):
        """
        A header isn't processed by L{HTTPClient.extractHeader} until it is
        confirmed in L{HTTPClient.lineReceived} that the header has been
        received completely.
        """
        c = ClientDriver()
        c.handleHeader = self.ourHandleHeader
        c.handleEndHeaders = self.ourHandleEndHeaders

        c.lineReceived(b'HTTP/1.0 201')
        c.lineReceived(b'Content-Length: 10')
        self.assertIdentical(c.length, None)
        self.assertFalse(self.handleHeaderCalled)
        self.assertFalse(self.handleEndHeadersCalled)

        # Signal end of headers.
        c.lineReceived(b'')
        self.assertTrue(self.handleHeaderCalled)
        self.assertTrue(self.handleEndHeadersCalled)

        self.assertEqual(c.length, 10)


    def test_noHeaders(self):
        """
        An HTTP request with no headers will not cause any calls to
        L{handleHeader} but will cause L{handleEndHeaders} to be called on
        L{HTTPClient} subclasses.
        """
        c = ClientDriver()
        c.handleHeader = self.ourHandleHeader
        c.handleEndHeaders = self.ourHandleEndHeaders
        c.lineReceived(b'HTTP/1.0 201')

        # Signal end of headers.
        c.lineReceived(b'')
        self.assertFalse(self.handleHeaderCalled)
        self.assertTrue(self.handleEndHeadersCalled)

        self.assertEqual(c.version, b'HTTP/1.0')
        self.assertEqual(c.status, b'201')


    def test_multilineHeaders(self):
        """
        L{HTTPClient} parses multiline headers by buffering header lines until
        an empty line or a line that does not start with whitespace hits
        lineReceived, confirming that the header has been received completely.
        """
        c = ClientDriver()
        c.handleHeader = self.ourHandleHeader
        c.handleEndHeaders = self.ourHandleEndHeaders

        c.lineReceived(b'HTTP/1.0 201')
        c.lineReceived(b'X-Multiline: line-0')
        self.assertFalse(self.handleHeaderCalled)
        # Start continuing line with a tab.
        c.lineReceived(b'\tline-1')
        c.lineReceived(b'X-Multiline2: line-2')
        # The previous header must be complete, so now it can be processed.
        self.assertTrue(self.handleHeaderCalled)
        # Start continuing line with a space.
        c.lineReceived(b' line-3')
        c.lineReceived(b'Content-Length: 10')

        # Signal end of headers.
        c.lineReceived(b'')
        self.assertTrue(self.handleEndHeadersCalled)

        self.assertEqual(c.version, b'HTTP/1.0')
        self.assertEqual(c.status, b'201')
        self.assertEqual(c.length, 10)



class Expect100ContinueServerTests(unittest.TestCase, ResponseTestMixin):
    """
    Test that the HTTP server handles 'Expect: 100-continue' header correctly.

    The tests in this class all assume a simplistic behavior where user code
    cannot choose to deny a request. Once ticket #288 is implemented and user
    code can run before the body of a POST is processed this should be
    extended to support overriding this behavior.
    """

    def test_HTTP10(self):
        """
        HTTP/1.0 requests do not get 100-continue returned, even if 'Expect:
        100-continue' is included (RFC 2616 10.1.1).
        """
        transport = StringTransport()
        channel = http.HTTPChannel()
        channel.requestFactory = DummyHTTPHandler
        channel.makeConnection(transport)
        channel.dataReceived(b"GET / HTTP/1.0\r\n")
        channel.dataReceived(b"Host: www.example.com\r\n")
        channel.dataReceived(b"Content-Length: 3\r\n")
        channel.dataReceived(b"Expect: 100-continue\r\n")
        channel.dataReceived(b"\r\n")
        self.assertEqual(transport.value(), b"")
        channel.dataReceived(b"abc")
        self.assertResponseEquals(
            transport.value(),
            [(b"HTTP/1.0 200 OK",
              b"Command: GET",
              b"Content-Length: 13",
              b"Version: HTTP/1.0",
              b"Request: /",
              b"'''\n3\nabc'''\n")])


    def test_expect100ContinueHeader(self):
        """
        If a HTTP/1.1 client sends a 'Expect: 100-continue' header, the server
        responds with a 100 response code before handling the request body, if
        any. The normal resource rendering code will then be called, which
        will send an additional response code.
        """
        transport = StringTransport()
        channel = http.HTTPChannel()
        channel.requestFactory = DummyHTTPHandler
        channel.makeConnection(transport)
        channel.dataReceived(b"GET / HTTP/1.1\r\n")
        channel.dataReceived(b"Host: www.example.com\r\n")
        channel.dataReceived(b"Expect: 100-continue\r\n")
        channel.dataReceived(b"Content-Length: 3\r\n")
        # The 100 continue response is not sent until all headers are
        # received:
        self.assertEqual(transport.value(), b"")
        channel.dataReceived(b"\r\n")
        # The 100 continue response is sent *before* the body is even
        # received:
        self.assertEqual(transport.value(), b"HTTP/1.1 100 Continue\r\n\r\n")
        channel.dataReceived(b"abc")
        response = transport.value()
        self.assertTrue(
            response.startswith(b"HTTP/1.1 100 Continue\r\n\r\n"))
        response = response[len(b"HTTP/1.1 100 Continue\r\n\r\n"):]
        self.assertResponseEquals(
            response,
            [(b"HTTP/1.1 200 OK",
              b"Command: GET",
              b"Content-Length: 13",
              b"Version: HTTP/1.1",
              b"Request: /",
              b"'''\n3\nabc'''\n")])


    def test_expect100ContinueWithPipelining(self):
        """
        If a HTTP/1.1 client sends a 'Expect: 100-continue' header, followed
        by another pipelined request, the 100 response does not interfere with
        the response to the second request.
        """
        transport = StringTransport()
        channel = http.HTTPChannel()
        channel.requestFactory = DummyHTTPHandler
        channel.makeConnection(transport)
        channel.dataReceived(
            b"GET / HTTP/1.1\r\n"
            b"Host: www.example.com\r\n"
            b"Expect: 100-continue\r\n"
            b"Content-Length: 3\r\n"
            b"\r\nabc"
            b"POST /foo HTTP/1.1\r\n"
            b"Host: www.example.com\r\n"
            b"Content-Length: 4\r\n"
            b"\r\ndefg")
        response = transport.value()
        self.assertTrue(
            response.startswith(b"HTTP/1.1 100 Continue\r\n\r\n"))
        response = response[len(b"HTTP/1.1 100 Continue\r\n\r\n"):]
        self.assertResponseEquals(
            response,
            [(b"HTTP/1.1 200 OK",
              b"Command: GET",
              b"Content-Length: 13",
              b"Version: HTTP/1.1",
              b"Request: /",
              b"'''\n3\nabc'''\n"),
             (b"HTTP/1.1 200 OK",
              b"Command: POST",
              b"Content-Length: 14",
              b"Version: HTTP/1.1",
              b"Request: /foo",
              b"'''\n4\ndefg'''\n")])
