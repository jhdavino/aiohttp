import asyncio
import mimetypes
import os

from . import hdrs
from .helpers import create_future
from .http_message import PayloadWriter
from .web_exceptions import (HTTPNotModified, HTTPOk, HTTPPartialContent,
                             HTTPRequestRangeNotSatisfiable)
from .web_response import StreamResponse


NOSENDFILE = bool(os.environ.get("AIOHTTP_NOSENDFILE"))


class SendfilePayloadWriter(PayloadWriter):

    def set_transport(self, transport):
        self._transport = transport

        if self._drain_waiter is not None:
            waiter, self._drain_maiter = self._drain_maiter, None
            if not waiter.done():
                waiter.set_result(None)

    def _write(self, chunk):
        self.output_size += len(chunk)
        self._buffer.append(chunk)

    def _sendfile_cb(self, fut, out_fd, in_fd,
                     offset, count, loop, registered):
        if registered:
            loop.remove_writer(out_fd)
        if fut.cancelled():
            return
        try:
            n = os.sendfile(out_fd, in_fd, offset, count)
            if n == 0:  # EOF reached
                n = count
        except (BlockingIOError, InterruptedError):
            n = 0
        except Exception as exc:
            fut.set_exception(exc)
            return

        if n < count:
            loop.add_writer(out_fd, self._sendfile_cb, fut, out_fd, in_fd,
                            offset + n, count - n, loop, True)
        else:
            fut.set_result(None)

    @asyncio.coroutine
    def sendfile(self, fobj, count):
        if self._transport is None:
            if self._drain_waiter is None:
                self._drain_waiter = create_future(self.loop)

            yield from self._drain_waiter

        out_socket = self._transport.get_extra_info("socket").dup()
        out_socket.setblocking(False)
        out_fd = out_socket.fileno()
        in_fd = fobj.fileno()
        offset = fobj.tell()

        loop = self.loop
        try:
            yield from loop.sock_sendall(out_socket, b''.join(self._buffer))
            fut = create_future(loop)
            self._sendfile_cb(fut, out_fd, in_fd, offset, count, loop, False)
            yield from fut
        finally:
            out_socket.close()

        self.output_size += count
        self._transport = None
        self._stream.release()

    @asyncio.coroutine
    def write_eof(self, chunk=b''):
        pass


class FileSender:
    """A helper that can be used to send files."""

    def __init__(self, *, resp_factory=StreamResponse, chunk_size=256*1024):
        self._response_factory = resp_factory
        self._chunk_size = chunk_size

    @asyncio.coroutine
    def _sendfile_system(self, request, resp, fobj, count):
        # Write count bytes of fobj to resp using
        # the os.sendfile system call.
        #
        # For details check
        # https://github.com/KeepSafe/aiohttp/issues/1177
        # See https://github.com/KeepSafe/aiohttp/issues/958 for details
        #
        # request should be a aiohttp.web.Request instance.
        # fobj should be an open file object.
        # count should be an integer > 0.

        transport = request.transport
        if transport.get_extra_info("sslcontext"):
            yield from self._sendfile_fallback(request, resp, fobj, count)
        else:
            writer = yield from resp.prepare(
                request, PayloadWriterFactory=SendfilePayloadWriter)
            yield from writer.sendfile(fobj, count)

    @asyncio.coroutine
    def _sendfile_fallback(self, request, resp, fobj, count):
        # Mimic the _sendfile_system() method, but without using the
        # os.sendfile() system call. This should be used on systems
        # that don't support the os.sendfile().

        # To avoid blocking the event loop & to keep memory usage low,
        # fobj is transferred in chunks controlled by the
        # constructor's chunk_size argument.

        yield from resp.prepare(request)

        resp.set_tcp_cork(True)
        try:
            chunk_size = self._chunk_size

            chunk = fobj.read(chunk_size)
            while True:
                yield from resp.write(chunk)
                count = count - chunk_size
                if count <= 0:
                    break
                chunk = fobj.read(min(chunk_size, count))
        finally:
            resp.set_tcp_nodelay(True)

    if hasattr(os, "sendfile") and not NOSENDFILE:  # pragma: no cover
        _sendfile = _sendfile_system
    else:  # pragma: no cover
        _sendfile = _sendfile_fallback

    @asyncio.coroutine
    def send(self, request, filepath):
        """Send filepath to client using request."""
        gzip = False
        if 'gzip' in request.headers.get(hdrs.ACCEPT_ENCODING, ''):
            gzip_path = filepath.with_name(filepath.name + '.gz')

            if gzip_path.is_file():
                filepath = gzip_path
                gzip = True

        st = filepath.stat()

        modsince = request.if_modified_since
        if modsince is not None and st.st_mtime <= modsince.timestamp():
            raise HTTPNotModified()

        ct, encoding = mimetypes.guess_type(str(filepath))
        if not ct:
            ct = 'application/octet-stream'

        status = HTTPOk.status_code
        file_size = st.st_size
        count = file_size

        try:
            rng = request.http_range
            start = rng.start
            end = rng.stop
        except ValueError:
            raise HTTPRequestRangeNotSatisfiable

        # If a range request has been made, convert start, end slice notation
        # into file pointer offset and count
        if start is not None or end is not None:
            status = HTTPPartialContent.status_code
            if start is None and end < 0:  # return tail of file
                start = file_size + end
                count = -end
            else:
                count = (end or file_size) - start

            if start + count > file_size:
                # rfc7233:If the last-byte-pos value is
                # absent, or if the value is greater than or equal to
                # the current length of the representation data,
                # the byte range is interpreted as the remainder
                # of the representation (i.e., the server replaces the
                # value of last-byte-pos with a value that is one less than
                # the current length of the selected representation).
                count = file_size - start

        resp = self._response_factory(status=status)
        resp.content_type = ct
        if encoding:
            resp.headers[hdrs.CONTENT_ENCODING] = encoding
        if gzip:
            resp.headers[hdrs.VARY] = hdrs.ACCEPT_ENCODING
        resp.last_modified = st.st_mtime

        resp.content_length = count
        with filepath.open('rb') as f:
            if start:
                f.seek(start)
            yield from self._sendfile(request, resp, f, count)

        return resp
