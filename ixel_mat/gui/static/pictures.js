// Pictures attached to a question. Each one is read here, made at most 2048 pixels on a side and
// drawn again, which drops whatever a camera wrote into it (where and when it was taken, the
// phone's name), then sent to Ixel, which keeps it in memory for 30 minutes. Only the models that
// can see pictures get them; the rest are told there's a picture they can't see.

import { api } from "./common.js";

export const MAX_PICTURES = 8;
const MAX_SIDE = 2048;
const MAX_BYTES = 3.5 * 1024 * 1024;  // under Ixel's 3.9 MB, and under what the APIs take once encoded
const MAX_FILE = 64 * 1024 * 1024;    // a file this big isn't read at all
const SHARP = ["image/png", "image/gif", "image/bmp", "image/x-icon"];  // screenshots and drawings stay PNG

function canvasOf(width, height) {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas;
}

const toBlob = (canvas, type, quality) => new Promise((resolve) => canvas.toBlob(resolve, type, quality));

async function decode(file) {
  try {
    return await createImageBitmap(file, { imageOrientation: "from-image" });  // upright, as it's seen
  } catch (e) {
    return createImageBitmap(file);  // a browser that doesn't take the option (it turns photos itself)
  }
}

// A file → { blob, width, height }, or an Error that says why not
export async function prepare(file) {
  const name = file.name || "That picture";
  if (!file.type.startsWith("image/")) throw new Error(`${name} isn't a picture.`);
  if (file.size > MAX_FILE) throw new Error(`${name} is too big to attach.`);
  let bitmap;
  try {
    bitmap = await decode(file);
  } catch (e) {
    throw new Error(`${name} can't be opened here: it's a kind this browser doesn't read (PNG, JPEG, WebP and ` +
      "GIF work), or too many pixels to open. Try a screenshot of it.");
  }
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const width = Math.max(1, Math.round(bitmap.width * scale));
  const height = Math.max(1, Math.round(bitmap.height * scale));
  const canvas = canvasOf(width, height);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#fff";  // see-through parts on white: some models see them as black, hiding dark lines
  ctx.fillRect(0, 0, width, height);
  ctx.drawImage(bitmap, 0, 0, width, height);
  bitmap.close();

  let blob = SHARP.includes(file.type) ? await toBlob(canvas, "image/png") : null;
  if (!blob || blob.size > MAX_BYTES) {
    for (const quality of [0.88, 0.8, 0.7, 0.6]) {  // a photo, or a screenshot too big as PNG
      blob = await toBlob(canvas, "image/jpeg", quality);
      if (blob && blob.size <= MAX_BYTES) break;
    }
  }
  if (!blob || blob.size > MAX_BYTES) throw new Error(`${name} is too detailed to send, even made smaller.`);
  return { blob, width, height };
}

// Sends a prepared picture to Ixel → its id there
export async function upload(blob) {
  let res;
  try {
    res = await api("/api/pictures", {
      method: "POST", body: blob, headers: { "Content-Type": blob.type }, signal: AbortSignal.timeout(60000),
    });
  } catch (e) {
    throw new Error(e.name === "TimeoutError" ? "Ixel didn't take the picture in time. Attach it again."
      : "Can't reach Ixel. Is it still running?");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Couldn't attach it (${res.status})`);
  return data.id;
}
