"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { FlvInputBuffer, FfmpegTsMuxer, VideoFrameClock } = require("../decrypt_stream.js");

function tag(type, payload) {
  const buffer = Buffer.alloc(15 + payload.length);
  buffer[0] = type;
  buffer.writeUIntBE(payload.length, 1, 3);
  payload.copy(buffer, 11);
  buffer.writeUInt32BE(payload.length + 11, buffer.length - 4);
  return buffer;
}
const header = Buffer.from([70, 76, 86, 1, 5, 0, 0, 0, 9, 0, 0, 0, 0]);
const video = tag(9, Buffer.from([0x17, 1, 0, 0, 0, 1, 2, 3]));
const audio = tag(8, Buffer.from([0xaf, 0, 0x12, 0x08]));

test("arbitrary byte splits preserve FLV packets exactly and defer startup", () => {
  const input = new FlvInputBuffer();
  const expected = Buffer.concat([header, audio, video]);
  const packets = [];
  for (let i = 0; i < expected.length; i++) {
    packets.push(...input.push(expected.subarray(i, i + 1)));
    if (i < expected.length - 1) assert.equal(input.ready(), false);
  }
  assert.equal(input.ready(), true);
  assert.deepEqual(packets.map(p => p.length), [header.length, audio.length, video.length]);
  assert.deepEqual(Buffer.concat(packets), expected);
});

test("FLV framing rejects invalid signatures, lengths, and oversize tags", () => {
  assert.throws(() => new FlvInputBuffer().push(Buffer.alloc(13)), /不是 FLV/);
  const broken = Buffer.from(video); broken[broken.length - 1] = 0;
  assert.throws(() => new FlvInputBuffer().push(Buffer.concat([header, broken])), /长度校验/);
  assert.throws(() => new FlvInputBuffer(16).push(Buffer.concat([header, video])), /超过/);
});

test("closing a backpressured encoder drains every accepted frame before EOF", () => {
  const writes = [];
  const pipe = new EventEmitter();
  pipe.writable = true;
  pipe.write = frame => { writes.push(frame.toString()); return writes.length > 1; };
  pipe.end = () => { pipe.writableEnded = true; writes.push("EOF"); };
  const muxer = Object.assign(Object.create(FfmpegTsMuxer.prototype), {
    videoIn: pipe, videoQueue: [], videoQueuedBytes: 0, videoDraining: false,
    maxPendingVideoBytes: 100, closed: false, closing: false,
  });
  assert.equal(muxer.enqueueVideo(Buffer.from("first")), true);
  assert.equal(muxer.enqueueVideo(Buffer.from("last")), true);
  muxer.close();
  assert.deepEqual(writes, ["first"]);
  assert.equal(muxer.enqueueVideo(Buffer.from("late")), false);
  pipe.emit("drain");
  assert.deepEqual(writes, ["first", "last", "EOF"]);
  assert.equal(muxer.videoQueuedBytes, 0);
});

test("source timestamps determine output duration at higher and lower source fps", () => {
  for (const sourceFps of [6, 12, 25]) {
    const clock = new VideoFrameClock(12);
    let emitted = 0;
    for (let index = 0; index < sourceFps * 30; index++) {
      emitted += clock.sample(Buffer.from([index % 256]), 5000 + index * 1000 / sourceFps).length;
    }
    assert.ok(Math.abs(emitted - 360) <= 1, `${sourceFps}fps produced ${emitted} frames`);
  }
});

test("timestamp discontinuities fail instead of making an unbounded duplicate queue", () => {
  const clock = new VideoFrameClock(12);
  clock.sample(Buffer.from([1]), 10000);
  assert.throws(() => clock.sample(Buffer.from([2]), 50000), /时间戳不连续/);
});
