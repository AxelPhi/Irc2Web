from collections import namedtuple
from datetime import datetime
import logging
import socket

import tornado
from tornado.concurrent import Future
import tornado.ioloop
import tornado.web
from tornado import gen


Event = namedtuple("Event", "raw line source nick user host code args")


class TwircUser(object):
    def __init__(self, nickname=None, oauth_token=None):
        self.nickname = nickname
        self.oauth_token = oauth_token


class TwircConnection(object):
    def __init__(self, host, port=6667):
        self.stream = None
        self.host = host
        self.port = port
        self.rate_threshold = 8

        self._socket = None
        self._keep_receiving = True
        self._rate_timestamp = datetime.now()
        self._rate_counter = 0
        self._rate_threshold = 20
        self._rate_timeout = 30
        self._ping_interval = 10

    @tornado.gen.coroutine
    def connect(self, nickname, oauth_token):
        logging.info('Starting connection to {}'.format(self.host))
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self.stream = tornado.iostream.IOStream(self._socket)
        yield self.stream.connect((self.host, self.port))
        yield self.login(nickname, oauth_token)
        yield self._receive()

    @tornado.gen.coroutine
    def login(self, nickname, oauth_token):
        logging.info('Logging in ...')
        oauth_token = oauth_token[oauth_token.startswith('oauth:') and len('oauth:'):]
        yield self._send('PASS oauth:{}'.format(oauth_token))
        yield self._send('NICK {}'.format(nickname))

    def disconnect(self):
        logging.info('Closing stream to {}'.format(self.host))
        self.stream.close()

    @tornado.gen.coroutine
    def _send(self, msg):
        yield self.stream.write(bytes(msg + '\r\n', 'utf-8'))
        logging.debug(' < {}'.format(msg))

    @tornado.gen.coroutine
    def _receive(self):

        event_dispatcher = {
            'PING': self.rcv_ping,
            'PONG': self.rcv_pong,
            'JOIN': self.rcv_join,
            'PART': self.rcv_part,
            'PRIVMSG': self.rcv_privmsg,
        }

        while self._keep_receiving:
            try:
                raw = yield self.stream.read_until(b'\n')
            except tornado.iostream.StreamClosedError:
                logging.info('Stream to {} closed'.format(self.host))
                self._keep_receiving = False
                continue
            raw = raw.rstrip(b'\r\n')
            line = raw.decode('utf-8')

            source = nick = user = host = None
            msg = line

            if line[0] == ':':
                pos = line.index(' ')
                source = line[1:pos]
                msg = line[pos + 1:]
                i = source.find('!')
                j = source.find('@')
                if i > 0 and j > 0:
                    nick = source[:i]
                    user = source[i + 1:j]
                    host = source[j + 1:]

            sp = msg.split(' :', 1)
            code, *args = sp[0].split(' ')
            if len(sp) == 2:
                args.append(sp[1])

            event = Event(raw, line, source, nick, user, host, code, args)
            event_dispatcher.get(event.code, self._log_event)(event)

    def _log_event(self, event):
        logging.debug(' > {}'.format(event))

    def rcv_ping(self, event):
        logging.debug(' > {} :{}'.format(event.code, event.args))
        self.send_pong(event.args[0])

    def rcv_pong(self, event):
        logging.debug(' > {} :{}'.format(event.code, event.args[1]))

    def rcv_join(self, event):
        logging.debug('Joined channel: {}'.format(event.args[0]))

    def rcv_part(self, event):
        logging.debug('Parted channel: {}'.format(event.args[0]))

    def rcv_privmsg(self, event):
        logging.debug('Received message in channel: [{}] {}'.format(event.args[0], event.args[1]))

    def send_pong(self, token):
        self._rate_check()
        self._send("PONG {}".format(token))

    def send_ping(self):
        self._rate_check()
        self._send('PING MARK{}'.format(int(datetime.now().timestamp())))
        if self._keep_receiving:
            tornado.ioloop.IOLoop.instance().call_later(self._ping_interval, self.send_ping)

    def send_privmsg(self, channel, msg):
        self._rate_check()
        self._send('PRIVMSG {} :{}'.format(channel, msg[:509]))

    def send_join(self, channel):
        self._rate_check()
        self._send('JOIN {}'.format(channel))

    def send_part(self, channel):
        self._rate_check()
        self._send('PART {}'.format(channel))

    def _rate_check(self):
        # TODO: Test this ... might not work as expected.
        now = datetime.now()
        if timedelta_to_seconds(now - self._rate_timestamp) < self._rate_timeout:
            self._rate_counter += 1
            if self._rate_counter == self._rate_threshold:
                raise IOError
        else:
            self._rate_counter = 0
            self._rate_timestamp = now


class ChannelBuffer(object):
    def __init__(self):
        self.buffer = []
        self._msg_future = Future()

    def await_msg(self):
        return self._msg_future

    def update_buffer(self, message):
        self.buffer.append(message)
        self._msg_future.set_result(self.buffer)
        self.buffer = []


class TwircAgent(object):
    def __init__(self, twirc_user):
        self.channels = {}
        self._twirc_user = twirc_user
        self._con = None

    def connect(self):
        self._con = TwircConnection('irc.twitch.tv')
        return self._con.connect()

    def disconnect(self):
        self._con.disconnect()

    def join(self, channel):
        pass

    def part(self, channel):
        pass

    def channels(self):
        return self.channels

    def send_msg(self, channel, message):
        if channel not in self.channels:
            return
        yield self._con.privmsg(channel, message)
        self.channels[channel].update_buffer(message)

    def receive_msg(self, channel, message):
        if channel not in self.channels:
            return
        self.channels[channel].update_buffer(message)


class ReceiveChatHandler(tornado.web.RequestHandler):

    def initialize(self, agent):
        self.agent = agent

    @gen.coroutine
    def get(self, channel_name):
        if channel_name not in self.agent.channels:
            logging.debug('Not in channel {} yet.')
            yield self.agent.join(channel_name)
        logging.debug('Waiting for new message')
        result = yield self.agent.channels[channel_name].await_msg()
        if self.request.connection.stream.closed():
            return
        self.write(result[0].encode('utf-8'))


class LoginRequestHandler(tornado.web.RequestHandler):

    @gen.coroutine
    def get(self):
        user = TwircUser(nickname='', oauth_token='')
        agent = TwircAgent(user)
        agents[1] = agent
        logging.debug('Trying to log in user: {}'.format(agent._twirc_user.nickname))
        yield agent.connect()


class SendChatHandler(tornado.web.RequestHandler):

    def get(self):
        pass


agents = {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    logging.info('Startup ...')

    # con = TwircConnection('irc.twitch.tv')
    # tornado.ioloop.IOLoop.instance().call_later(5, con.connect, '', '')

    cb = ChannelBuffer()

    user = TwircUser(nickname='', oauth_token='')
    agent = TwircAgent(user)
    # tornado.ioloop.IOLoop.instance().call_later(1, agent.connect)

    application = tornado.web.Application([
        (r"/login", LoginRequestHandler),
        (r"/channel/(.*)/receive", ReceiveChatHandler),
        (r"/channel/(.*)/send", SendChatHandler)
    ], debug=True)
    application.listen(37778)
    tornado.ioloop.IOLoop.instance().start()
