# -*- coding: utf-8 -*-
import logging
import time
import functools
import random

import tornado.ioloop
from tornado.ioloop import IOLoop

from client import Client
import nsq
import async


class Writer(Client):
    """
    A high-level producer class built on top of the `Tornado IOLoop <http://tornadoweb.org>`_
    supporting async publishing (``PUB`` & ``MPUB``) of messages to ``nsqd`` over the TCP protocol.

    Example publishing a message repeatedly using a Tornado IOLoop periodic callback::

        import nsq
        import tornado.ioloop
        import time

        def pub_message():
            writer.pub('test', time.strftime('%H:%M:%S'), finish_pub)

        def finish_pub(conn, data):
            print data

        writer = nsq.Writer(['127.0.0.1:4150'])
        tornado.ioloop.PeriodicCallback(pub_message, 1000).start()
        nsq.run()

    Example publshing a message from a Tornado HTTP request handler::

        import functools
        import tornado.httpserver
        import tornado.ioloop
        import tornado.options
        import tornado.web
        from nsq import Writer, Error
        from tornado.options import define, options

        class MainHandler(tornado.web.RequestHandler):
            @property
            def nsq(self):
                return self.application.nsq

            def get(self):
                topic = 'log'
                msg = 'Hello world'
                msg_cn = 'Hello 世界'

                self.nsq.pub(topic, msg) # pub
                self.nsq.mpub(topic, [msg, msg_cn]) # mpub

                # customize callback
                callback = functools.partial(self.finish_pub, topic=topic, msg=msg)
                self.nsq.pub(topic, msg, callback=callback)

                self.write(msg)

            def finish_pub(self, conn, data, topic, msg):
                if isinstance(data, Error):
                    # try to re-pub message again if pub failed
                    self.nsq.pub(topic, msg)

        class Application(tornado.web.Application):
            def __init__(self, handlers, **settings):
                self.nsq = Writer(['127.0.0.1:4150'])
                super(Application, self).__init__(handlers, **settings)

    :param nsqd_tcp_addresses: a sequence with elements of the form 'address:port' corresponding
        to the ``nsqd`` instances this writer should publish to

    :param **kwargs: passed to :class:`nsq.AsyncConn` initialization
    """

    def __init__(self, nsqd_tcp_addresses, **kwargs):
        if not isinstance(nsqd_tcp_addresses, (list, set, tuple)):
            assert isinstance(nsqd_tcp_addresses, (str, unicode))
            nsqd_tcp_addresses = [nsqd_tcp_addresses]
        assert nsqd_tcp_addresses

        self.nsqd_tcp_addresses = nsqd_tcp_addresses
        self.conns = {}
        self.conn_kwargs = kwargs
        self.name = "pynsq"

        tornado.ioloop.IOLoop.instance().add_callback(self._run)

    def _run(self):
        logging.info('starting writer...')
        self.connect()

    def pub(self, topic, msg, callback=None):
        self._pub('pub', topic, msg, callback)

    def mpub(self, topic, msg, callback=None):
        if isinstance(msg, (str, unicode)):
            msg = [msg]
        assert isinstance(msg, (list, set, tuple))

        self._pub('mpub', topic, msg, callback)

    def _pub(self, command, topic, msg, callback):
        if not callback:
            callback = functools.partial(self._finish_pub, command=command,
                                         topic=topic, msg=msg)

        if not self.conns:
            callback(None, nsq.SendError('no connections'))
            return

        conn = random.choice(self.conns.values())
        conn.callback_queue.append(callback)
        cmd = getattr(nsq, command)
        try:
            conn.send(cmd(topic, msg))
        except Exception:
            logging.exception('[%s] failed to send %s' % (conn.id, command))
            conn.close()

    def _on_connection_response(self, conn, data, **kwargs):
        if conn.callback_queue:
            callback = conn.callback_queue.pop(0)
            callback(conn, data)

    def connect(self):
        for addr in self.nsqd_tcp_addresses:
            host, port = addr.split(':')
            self.connect_to_nsqd(host, int(port))

    def connect_to_nsqd(self, host, port):
        assert isinstance(host, (str, unicode))
        assert isinstance(port, int)

        conn = async.AsyncConn(host, port, **self.conn_kwargs)
        conn.on('identify', self._on_connection_identify)
        conn.on('identify_response', self._on_connection_identify_response)
        conn.on('error', self._on_connection_response)
        conn.on('response', self._on_connection_response)
        conn.on('close', self._on_connection_close)
        conn.on('ready', self._on_connection_ready)
        conn.on('heartbeat', self.heartbeat)

        if conn.id in self.conns:
            return

        logging.info('[%s] connecting to nsqd', conn.id)
        conn.connect()
        conn.callback_queue = []

    def _on_connection_ready(self, conn, **kwargs):
        # re-check to make sure another connection didn't beat this one
        if conn.id in self.conns:
            logging.warning(
                '[%s] connected but another matching connection already exists', conn.id)
            conn.close()
            return
        self.conns[conn.id] = conn

    def _on_connection_close(self, conn, **kwargs):
        if conn.id in self.conns:
            del self.conns[conn.id]

        for callback in conn.callback_queue:
            try:
                callback(conn, nsq.ConnectionClosedError())
            except Exception:
                logging.exception('[%s] uncaught exception in callback', conn.id)

        logging.warning('[%s] connection closed', conn.id)
        logging.info('[%s] attempting to reconnect in 15s', conn.id)
        reconnect_callback = functools.partial(self.connect_to_nsqd,
                                               host=conn.host, port=conn.port)
        tornado.ioloop.IOLoop.instance().add_timeout(time.time() + 15, reconnect_callback)

    def _finish_pub(self, conn, data, command, topic, msg):
        if isinstance(data, nsq.Error):
            logging.error('[%s] failed to %s (%s, %s), data is %s',
                          conn.id, command, topic, msg, data)

    #
    # subclass overwriteable
    #

    def heartbeat(self, conn):
        logging.info('[%s] received heartbeat', conn.id)
