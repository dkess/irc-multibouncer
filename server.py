import asyncio
from aiohttp import web
from collections import defaultdict
from enum import Enum
from types import SimpleNamespace
from typing import Dict, NamedTuple, Optional, Set, Tuple, Union
import json

import IPython

UNIXPATH='s'
IRCSERVER = 'irc.ocf.berkeley.edu'

RETRY_WAIT = 5 # seconds

def mega_decode(b):
    try:
        return b.decode()
    except UnicodeDecodeError:
        try:
            return b.decode('cp1252')
        except UnicodeDecodeError:
            return b.decode('latin1')

def validate_channel_name(chan: str) -> bool:
    '''Enforce RFC 2812 channel name'''
    # TODO: this is not strict enough, but probably works 99% of the time
    if not 1 <= len(chan) <= 50:
        return False
    if chan[0] not in '&#+!':
        return False
    if ' ' in chan or ',' in chan:
        return False
    return True

def validate_nick(nick: str) -> bool:
    '''Enforce RFC 2812 nick name'''
    # TODO: not strict enough
    if ' ' in nick:
        return False
    return True

def strip_nick(fullname: str) -> str:
    return fullname.partition('!')[0]

class IRCError(Exception):
    def __init__(self, code: str, ircline: str) -> None:
        self.code = code
        self.ircline = ircline

    def json(self):
        return json.dumps({'code': self.code, 'ircline': self.ircline})

class ChannelState(Enum):
    # JOIN message has been sent
    JOINING = 0

    # Currently in channel
    JOINED = 1

    # PART message has been sent
    PARTING = 2

    # Not in channel (could also be due to kick)
    PARTED = 3

    # Not in channel, but waiting to join
    WAIT_JOIN = 4

ChannelEvents = NamedTuple('ChannelEvents', [('join', asyncio.Event), ('leave', asyncio.Event)])

class SessionData:
    def __init__(self):
        self.event = asyncio.Event()
        self.reader = None
        self.writer = None
        self.channels = {} # type: Dict[str, ChannelData]
        self.error = None

class ChannelData:
    def __init__(self, name: str, session: SessionData):
        self._state = ChannelState.PARTED

        self.name = name # type: str

        # True if the bot is configured to be in this channel
        self._target_state = True # type: bool

        # The IRC session associated with this channel
        self.session = session # type: SessionData

        # If waiting to join a channel, the task that will execute the join
        self._wait_join_task = None # type: Optional[asyncio.Task]

        self._wait_time = 0

        # trigerred when the channel is joined
        self.join_event = asyncio.Event()

        # triggered after leaving a channel
        self.leave_event = asyncio.Event()

    def changeState(self, newstate: ChannelState):
        oldstate = self._state
        self._state = newstate

        if oldstate == ChannelState.WAIT_JOIN and oldstate != newstate:
            self._wait_join_task = None

        if newstate == ChannelState.JOINED:
            self._wait_time = 0
            self.join_event.set()
            self.leave_event.clear()
        elif newstate == ChannelState.PARTED:
            self.join_event.clear()
            self.leave_event.set()

        self.onStateChange()

    def onStateChange(self):
        if self._target_state == True and self._state == ChannelState.PARTED:
            # first wait 0 seconds, but if that fails, wait longer
            self._wait_join_task = asyncio.ensure_future(self.joinAfterDelay(self._wait_time))
            self._wait_time = RETRY_WAIT
        elif self._target_state == False and self._state == ChannelState.JOINED:
            self.sendPart()

    def changeTargetState(self, newstate: bool):
        self._target_state = newstate

        self.onStateChange()

    async def joinAfterDelay(self, delay):
        await asyncio.sleep(delay)
        self.sendJoin()

    def sendJoin(self):
        self.session.writer.write('JOIN {}\r\n'.format(self.name).encode())
        self.changeState(ChannelState.JOINING)

    def sendPart(self):
        self.session.writer.write('PART {}\r\n'.format(self.name).encode())
        self.changeState(ChannelState.PARTING)

sessions = {} # type: Dict[str, SessionData]

routes = web.RouteTableDef()

async def irc_loop(nick: str, sd: SessionData):
    while not sd.reader.at_eof():
        line = mega_decode(await sd.reader.readline()).rstrip('\r\n')
        print('-> '+line)
        if line.startswith('PING :'):
            sd.writer.write(('PONG :'+line[6:]+'\r\n').encode())
        
        # JOIN message
        spl = line.split(' ')
        if len(spl) == 3 and spl[1] == 'JOIN':
            channel = spl[2][1:]
            if strip_nick(spl[0][1:]) == nick:
                sd.channels[channel].changeState(ChannelState.JOINED)
        
        # PART message
        spl = line.split(' ', 3)
        if len(spl) >= 3 and spl[1] == 'PART':
            channel = spl[2]
            if strip_nick(line[0][1:]) == nick:
                sd.channels[channel].changeState(ChannelState.PARTED)

        # KICK message
        spl = line.split(' ', 4)
        if len(spl) >= 4:
            kickee = spl[3]
            channel = spl[2]
            if kickee == nick:
                sd.channels[channel].changeState(ChannelState.PARTED)

        # Channel join error
        spl = line.split(' ', 4)
        if len(spl) >= 4:
            # possible error messages when joining a channel
            if spl[1] in {'474', '473', '403', '471', '475', '476', '405', '437'}:
                errnick = spl[2]
                channel = spl[3]
                if errnick == nick and channel in sd.channels:
                    sd.channels[channel].changeState(ChannelState.PARTED)

async def irc_initiate(nick: str, loop: asyncio.AbstractEventLoop) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection(IRCSERVER, 6697, ssl=True, loop=loop)
    writer.write('USER {nick} 0 * {nick}\r\nNICK {nick}\r\n'.format(nick=nick).encode())

    while not reader.at_eof():
        line = mega_decode(await reader.readline()).rstrip('\r\n')
        spl = line.split(' ', 2)
        if len(spl) >= 2:
            if spl[1] == '001':
                return reader, writer
            elif len(spl[1]) == 3 and spl[1][0] == '4':
                # Code beginning in 4 means error
                raise IRCError(spl[1], line)
            elif line.startswith('ERROR'):
                raise IRCError('', line)

    raise IRCFailure('', '')

@routes.put('/register')
async def register(request):
    try:
        j = await request.json()
        nick = j['nick']
    except:
        return web.Response(status=400)

    if nick not in sessions:
        sd = SessionData()
        sessions[nick] = sd

        try:
            sd.reader, sd.writer = await irc_initiate(nick, loop=request.loop)
            asyncio.ensure_future(irc_loop(nick, sd), loop=request.loop)
            sd.event.set()
            return web.Response(status=201)
        except IRCError as e:
            sd.error = e
            sd.event.set()
            del sessions[nick]
    else:
        sd = sessions[nick]

    await sd.event.wait()
    if sd.error:
        return web.Response(status=409, text=sd.error.json())

    return web.Response(status=204)

@routes.post('/join')
async def channeljoin(request):
    try:
        j = await request.json()
        nick = j['nick']
        channel = j['channel']
    except:
        return web.Response(status=400)
    
    if not validate_channel_name(channel) or not validate_nick(nick):
        return web.Response(Status=400)

    if nick not in sessions:
        return web.Response(status=404, text='"User does not exist"')
    sd = sessions[nick]
    await sd.event.wait()

    # TODO: maybe change this to a defaultdict of some kind
    if channel not in sd.channels:
        sd.channels[channel] = ChannelData(channel, sd)
        sd.channels[channel].changeTargetState(True)

    return web.Response(status=201)

@routes.post('/privmsg')
async def privmsg(request):
    try:
        j = await request.json()
        nick = j['nick']
        channel = j['channel']
        msg = j['msg']
    except:
        return web.Response(status=400)
    
    if not validate_channel_name(channel) or not validate_nick(nick):
        return web.Response(Status=400)

    if nick not in sessions:
        return web.Response(status=404, text='"User does not exist"')
    sd = sessions[nick]

    if channel not in sd.channels:
        return web.Response(status=404, text='"Channel was not joined"')

    await sd.channels[channel].join_event.wait()

    sd.writer.write('PRIVMSG {} :{}\r\n'.format(channel, msg).encode())
    return web.Response(status=200)


loop = asyncio.get_event_loop()
app = web.Application()
app.router.add_routes(routes)

#web.run_app(app, path=UNIXPATH, loop=loop)
web.run_app(app, port=8888)
