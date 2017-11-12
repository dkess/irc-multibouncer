import asyncio
from aiohttp import web
from collections import defaultdict
from enum import Enum
from types import SimpleNamespace
from typing import Dict, NamedTuple, Optional, Set, Tuple, Union
import json

UNIXPATH='s'
IRCSERVER = 'irc.ocf.berkeley.edu'

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

    # PART from the channel was received
    PARTED = 3

    # Kicked from channel
    KICKED = 4

    # Banned from channel
    BANNED = 5

    # Joined channel despite not being instructed to
    INVALID_JOINED = 6

    # Could not join channel due to error
    ERROR = 7

ChannelEvents = NamedTuple('ChannelEvents', [('join', asyncio.Event), ('leave', asyncio.Event)])

class SessionData:
    def __init__(self):
        self.event = asyncio.Event()
        self.reader = None
        self.writer = None
        self.channels = {}
        self.channel_events = defaultdict(lambda: ChannelEvents(asyncio.Event(), asyncio.Event()))
        self.error = None

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
            if line[1:].partition('!')[0] == nick:
                # TODO: error when joining channel
                # TODO: getting kicked from channel
                sd.channel_events[channel].join.set()
                if sd.channels[channel] == ChannelState.JOINING:
                    sd.channels[channel] = ChannelState.JOINED
                else:
                    sd.channels[channel] = ChannelState.INVALID_JOINED


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

    if channel not in sd.channels:
        sd.channels[channel] = ChannelState.JOINING
        sd.writer.write('JOIN {}\r\n'.format(channel).encode())

    if sd.channels[channel] == ChannelState.JOINING:
        await sd.channel_events[channel].join.wait()
    elif sd.channels[channel] == ChannelState.INVALID_JOIN:
        sd.channels[channel] = JOINED
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

    await sd.event.wait()

    if channel not in sd.channels or sd.channels[channel] == ChannelState.INVALID_JOINED:
        return web.Response(status=404, text='"Channel was not joined"')

    await sd.channel_events[channel].join.wait()

    sd.writer.write('PRIVMSG {} :{}\r\n'.format(channel, msg).encode())
    return web.Response(status=200)


loop = asyncio.get_event_loop()
app = web.Application()
app.router.add_routes(routes)

#web.run_app(app, path=UNIXPATH, loop=loop)
web.run_app(app, port=8888)
