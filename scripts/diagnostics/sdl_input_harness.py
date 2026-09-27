"""Bounded SDL event recorder; never imports GameWorker or sends gameplay input."""
import argparse
import ctypes as C
import json
import os
import struct
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sdl-library', required=True)
    parser.add_argument('--seconds', type=float, default=45)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120:
        parser.error('seconds must be 1..120')
    sdl = C.CDLL(args.sdl_library)
    sdl.SDL_CreateWindow.restype = C.c_void_p
    sdl.SDL_CreateWindow.argtypes = [C.c_char_p, C.c_int, C.c_int, C.c_int, C.c_int, C.c_uint]
    for name in ('SDL_RaiseWindow', 'SDL_DestroyWindow', 'SDL_GetWindowGrab'):
        getattr(sdl, name).argtypes = [C.c_void_p]
    sdl.SDL_GetKeyboardFocus.restype = C.c_void_p
    sdl.SDL_GetMouseFocus.restype = C.c_void_p
    sdl.SDL_GetError.restype = C.c_char_p
    if sdl.SDL_Init(0x20):
        raise RuntimeError(sdl.SDL_GetError())
    window = sdl.SDL_CreateWindow(b'DST input diagnostic', 0, 0, 400, 300, 4)
    if not window:
        raise RuntimeError(sdl.SDL_GetError())
    sdl.SDL_RaiseWindow(window)
    print(json.dumps({'ready': True, 'pid': os.getpid(), 'display': os.getenv('DISPLAY')}), flush=True)
    event = C.create_string_buffer(64)
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            while sdl.SDL_PollEvent(event):
                raw = event.raw
                kind, timestamp, window_id = struct.unpack_from('III', raw)
                row = {'monotonic': time.monotonic(), 'sdl_timestamp': timestamp,
                       'type': hex(kind), 'window': window_id,
                       'keyboard_focus': sdl.SDL_GetKeyboardFocus() == window,
                       'mouse_focus': sdl.SDL_GetMouseFocus() == window,
                       'grab': bool(sdl.SDL_GetWindowGrab(window))}
                if kind == 0x400:
                    row.update(zip(('buttons', 'x', 'y', 'dx', 'dy'),
                                   struct.unpack_from('Iiiii', raw, 12)))
                    row['device_source'] = 'unavailable in bundled SDL 2.0.0 event ABI'
                elif kind in (0x401, 0x402):
                    row.update(zip(('button', 'pressed', 'clicks', 'padding', 'x', 'y'),
                                   struct.unpack_from('BBBBii', raw, 12)))
                    row['device_source'] = 'unavailable in bundled SDL 2.0.0 event ABI'
                elif kind == 0x200:
                    row['window_event'] = raw[12]
                print(json.dumps(row), flush=True)
            time.sleep(.005)
    finally:
        sdl.SDL_DestroyWindow(window)
        sdl.SDL_Quit()


if __name__ == '__main__':
    main()
