// Sound for a question: recorded here or picked as a file, then sent to Ixel, which has the
// transcription service you set up (OpenAI or Groq) write it out. The words land in your question,
// where you read them before you ask; the sound itself isn't kept anywhere.

import { api } from "./common.js";

export const MAX_BYTES = 25 * 1024 * 1024;  // what the services take in one piece
export const MAX_RECORDING = 20 * 60;       // seconds: OpenAI's newer models take up to about 25 minutes
const TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];

export const canRecord = () => Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
  typeof MediaRecorder !== "undefined");

// Starts recording from the microphone → { stop() → Blob, cancel(), started }. `ended` is called if the
// recording stops by itself (the microphone unplugged, or its permission taken back).
export async function record(ended) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    throw new Error(e && e.name === "NotAllowedError"
      ? "The microphone isn't allowed here. Allow it for this page, or choose a sound file instead."
      : "There's no microphone this page can use. Choose a sound file instead.");
  }
  const release = () => { for (const track of stream.getTracks()) track.stop(); };
  const type = TYPES.find((t) => MediaRecorder.isTypeSupported(t)) || "";
  const chunks = [];
  let recorder;
  let asked = false;  // stop() or cancel() was called, rather than the recording ending by itself
  const done = new Promise((resolve) => {
    try {
      recorder = new MediaRecorder(stream, { ...(type ? { mimeType: type } : {}), audioBitsPerSecond: 32000 });
      recorder.addEventListener("dataavailable", (e) => { if (e.data.size) chunks.push(e.data); });
      recorder.addEventListener("stop", () => {
        release();
        resolve(new Blob(chunks, { type: recorder.mimeType || type }));
        if (!asked && ended) ended();
      });
      recorder.start(1000);
    } catch (e) {
      recorder = null;
    }
  });
  if (!recorder) {
    release();  // the microphone isn't left on
    throw new Error("This browser can't record sound here. Choose a sound file instead.");
  }
  return {
    started: performance.now(),
    async stop() {
      asked = true;
      if (recorder.state !== "inactive") recorder.stop();
      return done;
    },
    cancel() {
      asked = true;
      if (recorder.state !== "inactive") recorder.stop();
      release();
    },
  };
}

// Sends sound to Ixel to be written out by `expect` (the service the page named) → { text, service }.
// When the settings changed meanwhile, nothing is sent: the Error's code is "sound_service_changed".
export async function transcribe(blob, signal, expect) {
  if (blob.size > MAX_BYTES) {
    throw new Error("That's over 25 MB of sound, more than the service takes at once. Send a shorter piece.");
  }
  let res;
  try {
    res = await api(`/api/sound?expect=${encodeURIComponent(expect || "")}`, {
      method: "POST", body: blob, signal, headers: { "Content-Type": "application/octet-stream" },
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new Error("Can't reach Ixel. Is it still running?");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw Object.assign(new Error(data.error || `Couldn't write it out (${res.status})`), { code: data.code || "" });
  }
  return data;
}

// A recording saved as a file, for when it couldn't be written out
export function save(blob) {
  const url = URL.createObjectURL(blob);
  const ext = /mp4/.test(blob.type) ? "m4a" : /ogg/.test(blob.type) ? "ogg" : "webm";
  const link = Object.assign(document.createElement("a"), { href: url, download: `Ixel recording.${ext}` });
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

export function clock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
