const NAMED_KEYS: Record<string, number> = {
  enter: 0x0d,
  return: 0x0d,
  escape: 0x1b,
  esc: 0x1b,
  tab: 0x09,
  backspace: 0x08,
  space: 0x20,
  spacebar: 0x20,
  capslock: 0x14,

  left: 0x25,
  up: 0x26,
  right: 0x27,
  down: 0x28,
  home: 0x24,
  end: 0x23,
  pageup: 0x21,
  pagedown: 0x22,
  insert: 0x2d,
  delete: 0x2e,
  del: 0x2e,

  printscreen: 0x2c,
  scrolllock: 0x91,
  pause: 0x13,
  numlock: 0x90,

  ctrl: 0x11,
  control: 0x11,
  lctrl: 0xa2,
  rctrl: 0xa3,
  shift: 0x10,
  lshift: 0xa0,
  rshift: 0xa1,
  alt: 0x12,
  menu: 0x12,
  lalt: 0xa4,
  ralt: 0xa5,
  win: 0x5b,
  windows: 0x5b,
  lwin: 0x5b,
  rwin: 0x5c,

  numpad0: 0x60,
  numpad1: 0x61,
  numpad2: 0x62,
  numpad3: 0x63,
  numpad4: 0x64,
  numpad5: 0x65,
  numpad6: 0x66,
  numpad7: 0x67,
  numpad8: 0x68,
  numpad9: 0x69,
  multiply: 0x6a,
  add: 0x6b,
  subtract: 0x6d,
  decimal: 0x6e,
  divide: 0x6f,

  semicolon: 0xba,
  equals: 0xbb,
  comma: 0xbc,
  minus: 0xbd,
  period: 0xbe,
  slash: 0xbf,
  backtick: 0xc0,
  grave: 0xc0,
  openbracket: 0xdb,
  backslash: 0xdc,
  closebracket: 0xdd,
  quote: 0xde,
};

for (let i = 1; i <= 24; i++) {
  NAMED_KEYS[`f${i}`] = 0x6f + i;
}

export function resolveVk(key: string): number {
  const normalized = key.trim().toLowerCase();
  if (normalized in NAMED_KEYS) return NAMED_KEYS[normalized];

  if (normalized.length === 1) {
    const ch = normalized;
    if (ch >= "a" && ch <= "z") return ch.toUpperCase().charCodeAt(0);
    if (ch >= "0" && ch <= "9") return ch.charCodeAt(0);
  }

  throw new Error(`Unknown key name: "${key}"`);
}
