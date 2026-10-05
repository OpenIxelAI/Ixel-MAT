// Video for a question, which never leaves this page whole: a few frames are taken from it here and
// attached like pictures, and its sound is taken out here, made small, and written out like any sound.

import { MAX_BYTES as MAX_SOUND_BYTES } from "./sound.js";

export const MAX_FRAMES = 6;
const FRAME_SIDE = 2048;
const RATE = 16000;                       // what speech recognition listens at anyway
// The browser takes all of a video's sound apart at once, every channel at full quality (around 1 GB of
// memory for 20 minutes of stereo), so a longer video isn't taken apart at all: up to a minute over is cut
export const MAX_SOUND_SECONDS = 20 * 60;
const OVER_SECONDS = 60;
const PIECE_SECONDS = 10 * 60;            // a piece is at most about 19 MB as WAV, under the services' 25 MB
const MAX_SOUND_FILE = 1024 * 1024 * 1024; // the whole file is read to take its sound out
let taking = Promise.resolve();           // one at a time: a cancelled one runs on until it's done

export const isVideo = (file) => file.type.startsWith("video/") ||
  (!file.type && /\.(mp4|m4v|mov|webm|mkv|ogv)$/i.test(file.name || ""));

const nameOf = (file) => file.name || "That video";

function once(target, ok, fail, ms, why) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { done(); reject(new Error(why)); }, ms);
    const pass = () => { done(); resolve(); };
    const stop = () => { done(); reject(new Error(why)); };
    function done() {
      clearTimeout(timer);
      target.removeEventListener(ok, pass);
      target.removeEventListener(fail, stop);
    }
    target.addEventListener(ok, pass);
    target.addEventListener(fail, stop);
  });
}

async function open(file, until = "loadeddata") {
  const video = document.createElement("video");
  const url = URL.createObjectURL(file);
  video.muted = true;
  video.preload = "auto";
  video.playsInline = true;
  const close = () => {
    video.removeAttribute("src");
    video.load();
    URL.revokeObjectURL(url);
  };
  const cant = `No frames were taken from ${nameOf(file)}: this browser can't play that kind of video.`;
  try {
    const ready = once(video, until, "error", 20000, cant);
    video.src = url;
    await ready;
    if (!Number.isFinite(video.duration)) {
      // A recording made in a browser often doesn't say how long it is: looking at its end finds out
      const known = once(video, "seeked", "error", 20000, cant).catch(() => {});  // else: its first frame only
      video.currentTime = 1e9;
      await known;
    }
  } catch (e) {
    close();
    throw e;
  }
  return { video, close };
}

async function seek(video, time) {
  const there = once(video, "seeked", "error", 20000, "The video stopped being readable partway.");
  video.currentTime = time;
  await there;
}

const toBlob = (canvas) => new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));

export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const m = Math.floor(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
    : `${m}:${String(s % 60).padStart(2, "0")}`;
}

// When to take `count` frames from a video `duration` seconds long: spread through it, and at least a frame
// (of 30 a second) apart, so a very short one gives fewer, each different
export function frameTimes(duration, count) {
  if (!duration) return [0];
  const last = Math.max(0, duration - 0.001);
  const times = [];
  for (let i = 0; i < count; i += 1) {
    const t = Math.min(((i + 0.5) * duration) / count, last);
    if (!times.length || t - times[times.length - 1] >= 1 / 30) times.push(t);
  }
  return times;
}

// `count` frames spread through the video → { files: [File] (JPEG, named for where they're from), seconds }.
// No files when it has no picture (sound saved as a video).
export async function frames(file, count) {
  const { video, close } = await open(file);
  try {
    const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;
    if (!video.videoWidth) return { files: [], seconds: duration };
    const times = frameTimes(duration, count);
    const scale = Math.min(1, FRAME_SIDE / Math.max(video.videoWidth, video.videoHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    const ctx = canvas.getContext("2d");
    const out = [];
    for (const time of times) {
      await seek(video, time);
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const blob = await toBlob(canvas);
      if (!blob) throw new Error(`Couldn't take a frame from ${nameOf(file)}.`);
      out.push(new File([blob], `${nameOf(file)} at ${clock(time)}.jpg`, { type: "image/jpeg" }));
    }
    return { files: out, seconds: duration };
  } finally {
    close();
  }
}

// How long it is, in seconds (0 when the browser can't tell)
export async function length(file) {
  const { video, close } = await open(file, "loadedmetadata");
  const seconds = Number.isFinite(video.duration) ? video.duration : 0;
  close();
  return seconds;
}

// A WAV file of mono 16-bit sound
function wav(samples) {
  const out = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const text = (at, s) => { for (let i = 0; i < s.length; i += 1) out.setUint8(at + i, s.charCodeAt(i)); };
  text(0, "RIFF");
  out.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  out.setUint32(16, 16, true);
  out.setUint16(20, 1, true);          // PCM
  out.setUint16(22, 1, true);          // one channel
  out.setUint32(24, RATE, true);
  out.setUint32(28, RATE * 2, true);
  out.setUint16(32, 2, true);
  out.setUint16(34, 16, true);
  text(36, "data");
  out.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i += 1) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    out.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Blob([out], { type: "audio/wav" });
}

// A WebM that holds only sound (the browser calls every .webm a video, the ones it records sound into too) and
// is small enough to send as it is, as any sound file is. Read from its list of tracks, which has to be whole
// in the file's first megabyte: anything unexpected, and it's taken for a video (its frames and sound taken out
// here, so a video never goes whole by mistake).
export async function soundOnly(file) {
  if (file.size > MAX_SOUND_BYTES || !(/webm|matroska/i.test(file.type) || /\.(webm|mka)$/i.test(file.name || ""))) {
    return false;
  }
  const bytes = new Uint8Array(await file.slice(0, 1024 * 1024).arrayBuffer());
  try {
    return tracksAreSound(bytes);
  } catch (e) {
    return false;
  }
}

// EBML (the format WebM is written in): an element is an ID, its size, then its data
const EBML_HEAD = 0x1a45dfa3;
const SEGMENT = 0x18538067;
const TRACKS = 0x1654ae6b;
const CLUSTER = 0x1f43b675;
const TRACK = 0xae;
const TRACK_TYPE = 0x83;
const SOUND = 2;
const SUBTITLES = 0x11;

function vint(bytes, at, keepMarker) {
  const first = bytes[at];
  if (first === undefined || first === 0) return null;
  const width = Math.clz32(first) - 23;  // the leading zeros say how many bytes it takes
  if (at + width > bytes.length) return null;
  let value = keepMarker ? first : first & (0xff >> width);
  let unknown = !keepMarker && value === 0xff >> width;
  for (let i = 1; i < width; i += 1) {
    value = value * 256 + bytes[at + i];
    if (bytes[at + i] !== 0xff) unknown = false;
  }
  return { value, width, unknown };
}

function element(bytes, at) {
  const id = vint(bytes, at, true);
  if (!id || id.width > 4) return null;
  const size = vint(bytes, at + id.width, false);
  if (!size) return null;
  const start = at + id.width + size.width;
  return { id: id.value, start, end: size.unknown ? Infinity : start + size.value, unknown: size.unknown };
}

function children(bytes, parent, each) {
  for (let at = parent.start; at < parent.end;) {
    const child = element(bytes, at);
    if (!child || child.unknown || child.end > parent.end || child.end > bytes.length) throw new Error("unreadable");
    each(child);
    at = child.end;
  }
}

function tracksAreSound(bytes) {
  const head = element(bytes, 0);
  if (!head || head.id !== EBML_HEAD || head.unknown) return false;
  const segment = element(bytes, head.end);
  if (!segment || segment.id !== SEGMENT) return false;  // (its size may be unknown: a recording's often is)
  let tracks = null;
  for (let at = segment.start; at < bytes.length && !tracks;) {
    const child = element(bytes, at);
    if (!child || child.unknown || child.id === CLUSTER) return false;  // what's in it, before saying what it is
    if (child.id === TRACKS) tracks = child;
    at = child.end;
  }
  if (!tracks || tracks.end > bytes.length) return false;
  let sound = false;
  children(bytes, tracks, (entry) => {
    if (entry.id !== TRACK) return;
    let type = null;
    children(bytes, entry, (field) => {
      if (field.id !== TRACK_TYPE) return;
      type = 0;
      for (let i = field.start; i < field.end; i += 1) type = type * 256 + bytes[i];
    });
    if (type === SOUND) sound = true;
    else if (type !== SUBTITLES) throw new Error("not only sound");
  });
  return sound;
}

// The video's sound, small enough to send → { pieces: [Blob], seconds, cut }, or null when it has none
// (`seconds`: how long it is; 0 when that isn't known)
export async function soundOf(file, seconds = 0) {
  const name = nameOf(file);
  if (seconds > MAX_SOUND_SECONDS + OVER_SECONDS) {  // all of it is read to take any of it out
    throw new Error(`${name} is over ${MAX_SOUND_SECONDS / 60} minutes long, too long to take its sound out here. ` +
      "Save the part you need as a sound file and choose that under Sound.");
  }
  if (file.size > MAX_SOUND_FILE || (!seconds && file.size > MAX_SOUND_BYTES)) {
    throw new Error(`${name} is too big to take its sound out here${seconds ? "" : " when this browser can't " +
      "tell how long it is"}. Save the part you need as a sound file and choose that under Sound.`);
  }
  const before = taking;
  let done;
  taking = new Promise((resolve) => { done = resolve; });
  try {
    await before;
    let decoded;
    try {
      decoded = await new OfflineAudioContext(1, 1, RATE).decodeAudioData(await file.arrayBuffer());  // at 16 kHz
    } catch (e) {
      return null;  // no sound, or a kind this browser can't take apart
    }
    const length = Math.min(decoded.length, MAX_SOUND_SECONDS * RATE);
    const mono = new Float32Array(length);
    for (let c = 0; c < decoded.numberOfChannels; c += 1) {
      const channel = decoded.getChannelData(c);
      for (let i = 0; i < length; i += 1) mono[i] += channel[i] / decoded.numberOfChannels;
    }
    // Equal pieces, so none is a sliver too short to write out
    const size = Math.ceil(length / Math.max(1, Math.ceil(length / (PIECE_SECONDS * RATE))));
    const pieces = [];
    for (let at = 0; at < length; at += size) pieces.push(wav(mono.subarray(at, at + size)));
    return { pieces, seconds: length / RATE, cut: decoded.length > length };
  } finally {
    done();
  }
}
