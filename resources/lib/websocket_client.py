from __future__ import (
    division, absolute_import, print_function, unicode_literals
)

import json
import threading
import time
import traceback

import xbmc
import xbmcaddon
import xbmcgui
import websocket

from .jellyfin import API
from .functions import play_action
from .lazylogger import LazyLogger
from .jsonrpc import JsonRpc
from .kodi_utils import HomeWindow
from .utils import load_user_details

log = LazyLogger(__name__)

# Heartbeat watchdog. We subscribe to a periodic server push (ScheduledTasks
# info) so the server is guaranteed to send us something on a known interval.
# If nothing arrives for HEARTBEAT_TIMEOUT seconds the connection is treated as
# dead and torn down so the reconnect loop runs. This detects a stale
# ("half-open") socket - e.g. after a NAT/firewall idle timeout or IP change -
# that otherwise leaves run_forever() blocked forever. It uses ordinary data
# messages rather than WebSocket ping frames, so the Jellyfin server does not
# log them (the spam that caused ping_interval to be reverted in d312241).
HEARTBEAT_PERIOD_MS = 30000
HEARTBEAT_CHECK = 30
HEARTBEAT_TIMEOUT = 90


class WebSocketClient(threading.Thread):

    def __init__(self, library_change_monitor):

        threading.Thread.__init__(self)

        self._client = None
        self._stop_websocket = False
        self._library_monitor = library_change_monitor
        self.monitor = xbmc.Monitor()

        self.websocket_error = False
        self.last_message_time = time.time()
        self._watchdog_timer = None
        self._keepalive_timer = None
        settings = xbmcaddon.Addon()
        user_details = load_user_details()

        self.api = API(
            settings.getSetting('server_address'),
            user_details.get('user_id'),
            user_details.get('token')
        )

    def on_message(self, ws, message):

        # Any inbound traffic proves the connection is still alive.
        self.last_message_time = time.time()

        try:
            result = json.loads(message)
            message_type = result['MessageType']

            if message_type == 'Play':
                data = result['Data']
                self._play(data)

            elif message_type == 'Playstate':
                data = result['Data']
                self._playstate(data)

            elif message_type == "UserDataChanged":
                data = result['Data']
                self._library_changed(data)

            elif message_type == "LibraryChanged":
                data = result['Data']
                self._library_changed(data)

            elif message_type == "GeneralCommand":
                data = result['Data']
                self._general_commands(data)

            else:
                log.debug("WebSocket Message Type: {0}".format(message))

        except Exception:
            log.error(
                "Exception processing WebSocket message:\n{0}".format(
                    traceback.format_exc()))

    def _library_changed(self, data):
        log.debug("Library_Changed: {0}".format(data))
        self._library_monitor.check_for_updates()

    def _play(self, data):

        item_ids = data['ItemIds']
        command = data['PlayCommand']

        if command == 'PlayNow':
            home_screen = HomeWindow()
            home_screen.set_property("skip_select_user", "true")

            startat = data.get('StartPositionTicks', -1)
            log.debug("WebSocket Message PlayNow: {0}".format(data))

            media_source_id = data.get("MediaSourceId", "")
            subtitle_stream_index = data.get("SubtitleStreamIndex", None)
            audio_stream_index = data.get("AudioStreamIndex", None)

            start_index = data.get("StartIndex", 0)

            if start_index > 0 and start_index < len(item_ids):
                item_ids = item_ids[start_index:]

            if len(item_ids) == 1:
                item_ids = item_ids[0]

            params = {}
            params["item_id"] = item_ids
            params["auto_resume"] = str(startat)
            params["media_source_id"] = media_source_id
            params["subtitle_stream_index"] = subtitle_stream_index
            params["audio_stream_index"] = audio_stream_index
            play_action(params)

    def _playstate(self, data):

        command = data['Command']
        player = xbmc.Player()

        actions = {

            'Stop': player.stop,
            'Unpause': player.pause,
            'Pause': player.pause,
            'PlayPause': player.pause,
            'NextTrack': player.playnext,
            'PreviousTrack': player.playprevious
        }
        if command == 'Seek':

            if player.isPlaying():
                seek_to = data['SeekPositionTicks']
                seek_time = seek_to / 10000000.0
                player.seekTime(seek_time)
                log.debug("Seek to {0}".format(seek_time))

        elif command in actions:
            actions[command]()
            log.debug("Command: {0} completed".format(command))

        else:
            log.debug("Unknown command: {0}".format(command))
            return

    def _general_commands(self, data):

        command = data['Name']
        arguments = data['Arguments']

        if command in ('Mute',
                       'Unmute',
                       'SetVolume',
                       'SetSubtitleStreamIndex',
                       'SetAudioStreamIndex',
                       'SetRepeatMode'):

            player = xbmc.Player()
            # These commands need to be reported back
            if command == 'Mute':
                xbmc.executebuiltin('Mute')

            elif command == 'Unmute':
                xbmc.executebuiltin('Mute')

            elif command == 'SetVolume':
                volume = arguments['Volume']
                xbmc.executebuiltin(
                    'SetVolume({}[,showvolumebar])'.format(volume)
                )

            elif command == 'SetAudioStreamIndex':
                index = int(arguments['Index'])
                player.setAudioStream(index - 1)

            elif command == 'SetSubtitleStreamIndex':
                index = int(arguments['Index'])
                player.setSubtitleStream(index - 1)

            elif command == 'SetRepeatMode':
                mode = arguments['RepeatMode']
                xbmc.executebuiltin('xbmc.PlayerControl({})'.format(mode))

        elif command == 'DisplayMessage':

            # header = arguments['Header']
            text = arguments['Text']
            # show notification here
            log.debug("WebSocket DisplayMessage: {0}".format(text))
            xbmcgui.Dialog().notification("JellyCon", text)

        elif command == 'SendString':

            params = {

                'text': arguments['String'],
                'done': False
            }
            JsonRpc('Input.SendText').execute(params)

        elif command in ('MoveUp', 'MoveDown', 'MoveRight', 'MoveLeft'):
            # Commands that should wake up display
            actions = {

                'MoveUp': "Input.Up",
                'MoveDown': "Input.Down",
                'MoveRight': "Input.Right",
                'MoveLeft': "Input.Left"
            }
            JsonRpc(actions[command]).execute()

        elif command == 'GoHome':
            JsonRpc('GUI.ActivateWindow').execute({'window': "home"})

        elif command == "Guide":
            JsonRpc('GUI.ActivateWindow').execute({'window': "tvguide"})

        else:
            builtin = {

                'ToggleFullscreen': 'Action(FullScreen)',
                'ToggleOsdMenu': 'Action(OSD)',
                'ToggleContextMenu': 'Action(ContextMenu)',
                'Select': 'Action(Select)',
                'Back': 'Action(back)',
                'PageUp': 'Action(PageUp)',
                'NextLetter': 'Action(NextLetter)',
                'GoToSearch': 'VideoLibrary.Search',
                'GoToSettings': 'ActivateWindow(Settings)',
                'PageDown': 'Action(PageDown)',
                'PreviousLetter': 'Action(PrevLetter)',
                'TakeScreenshot': 'TakeScreenshot',
                'ToggleMute': 'Mute',
                'VolumeUp': 'Action(VolumeUp)',
                'VolumeDown': 'Action(VolumeDown)',
            }
            if command in builtin:
                xbmc.executebuiltin(builtin[command])

    def on_open(self, ws):
        log.debug("Connected")
        self.last_message_time = time.time()
        self.api.post_capabilities()
        self._cancel_timers()
        self.send_keepalive(ws)
        self.subscribe_heartbeat(ws)
        self.schedule_watchdog(ws)

    def on_error(self, ws, error):
        self.websocket_error = True
        log.error("WebSocket error: {0}".format(error))

    def on_close(self, ws, close_status_code=None, close_msg=None):
        log.debug(
            "WebSocket closed (code={0}, reason={1})".format(
                close_status_code, close_msg))

    def run(self):

        while self.api.token is None or self.api.token == "":
            if self.monitor.waitForAbort(11):
                return

        # Get the appropriate prefix for the websocket
        settings = xbmcaddon.Addon()
        server = settings.getSetting('server_address')
        if "https://" in server:
            server = server.replace('https://', 'wss://')
        else:
            server = server.replace('http://', 'ws://')

        websocket_url = "{}/socket".format(server)
        log.debug("websocket url: {0}".format(websocket_url))

        log.debug("Starting WebSocketClient")

        while not self.monitor.abortRequested():

            self.websocket_error = False
            self._cancel_timers()

            headers = self.api.headers
            self._client = websocket.WebSocketApp(
                websocket_url,
                header=headers,
                on_open=lambda ws: self.on_open(ws),
                on_message=lambda ws, message: self.on_message(ws, message),
                on_error=lambda ws, error: self.on_error(ws, error),
                on_close=lambda ws, code, reason: self.on_close(
                    ws, code, reason))

            log.debug("Opening WebSocket connection")
            try:
                self._client.run_forever()
            except Exception:
                log.error(
                    "WebSocket loop failed:\n{0}".format(
                        traceback.format_exc()))

            log.debug("WebSocket connection ended")

            if self._stop_websocket:
                break

            if self.monitor.waitForAbort(20):
                # Abort was requested, exit
                break

            log.debug("Reconnecting WebSocket")

        log.debug("WebSocketClient Stopped")

    def stop_client(self):

        self._stop_websocket = True
        self._cancel_timers()
        if self._client is not None:
            self._client.close()
        log.debug("Stopping WebSocket (stop_client called)")

    def send_keepalive(self, ws):
        # Stop the keepalive cycle if an error has been detected
        if self.websocket_error or ws is not self._client:
            return
        keepalive_payload = json.dumps({"MessageType": "KeepAlive", "Data": 30})
        # Send the keepalive, or register an error
        try:
            ws.send(keepalive_payload)
        except Exception as error:
            self.websocket_error = True
            log.error("WebSocket keepalive failed: {0}".format(error))
            try:
                ws.close()
            except Exception:
                pass
            return
        # Schedule the next message
        self.schedule_keepalive(ws)

    def schedule_keepalive(self, ws):
        # Schedule a keepalive message in 30 seconds
        timer = threading.Timer(30, self.send_keepalive, kwargs={'ws': ws})
        timer.start()
        self._keepalive_timer = timer

    def _cancel_timers(self):
        if self._watchdog_timer:
            self._watchdog_timer.cancel()
            self._watchdog_timer = None
        if self._keepalive_timer:
            self._keepalive_timer.cancel()
            self._keepalive_timer = None

    def subscribe_heartbeat(self, ws):
        # Ask the server to push ScheduledTasksInfo every HEARTBEAT_PERIOD_MS.
        # Data is "dueTimeMs,periodMs"; this gives us a steady inbound signal
        # that the watchdog can use to detect a dead connection.
        payload = json.dumps({
            "MessageType": "ScheduledTasksInfoStart",
            "Data": "0,{0}".format(HEARTBEAT_PERIOD_MS),
        })
        try:
            ws.send(payload)
        except Exception as error:
            log.error(
                "Failed to subscribe to WebSocket heartbeat: {0}".format(
                    error))

    def schedule_watchdog(self, ws):
        # Check connection liveness in HEARTBEAT_CHECK seconds
        timer = threading.Timer(
            HEARTBEAT_CHECK, self.check_watchdog, kwargs={'ws': ws})
        timer.start()
        self._watchdog_timer = timer

    def check_watchdog(self, ws):
        # Stop stale timers from previous connections / after shutdown
        if self._stop_websocket or ws is not self._client:
            return
        elapsed = time.time() - self.last_message_time
        if elapsed > HEARTBEAT_TIMEOUT:
            # No inbound traffic for too long - the socket is most likely
            # half-open. Close it so the blocked run_forever() returns and
            # the reconnect loop runs.
            log.debug(
                "Watchdog: no message for {0:.0f}s, reconnecting".format(
                    elapsed))
            try:
                ws.close()
            except Exception:
                pass
            return
        # Still alive - keep watching
        self.schedule_watchdog(ws)
