#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const https = require("https");
const { spawn } = require("child_process");

const DEFAULT_LIBFFMPEG_URL = "https://s4.ssl.qhres2.com/!feb3e5fa/libffmpeg.js";
const DEFAULT_CACHE_DIR = path.join(__dirname, ".cache");
const DEFAULT_LIBFFMPEG_PATH = path.join(DEFAULT_CACHE_DIR, "libffmpeg.js");

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item.startsWith("--")) {
      continue;
    }
    const key = item.slice(2);
    const next = argv[i + 1];
    if (!next || next.startsWith("--")) {
      args[key] = true;
      continue;
    }
    args[key] = next;
    i += 1;
  }
  return args;
}

function log(message, quiet = false) {
  if (!quiet) {
    process.stderr.write(`${message}\n`);
  }
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function downloadFile(url, filePath) {
  return new Promise((resolve, reject) => {
    ensureDir(path.dirname(filePath));
    const request = https.get(url, (response) => {
      if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        response.resume();
        downloadFile(response.headers.location, filePath).then(resolve, reject);
        return;
      }
      if (response.statusCode !== 200) {
        reject(new Error(`下载 libffmpeg.js 失败: HTTP ${response.statusCode}`));
        response.resume();
        return;
      }
      const file = fs.createWriteStream(filePath);
      response.pipe(file);
      file.on("finish", () => {
        file.close(resolve);
      });
      file.on("error", reject);
    });
    request.on("error", reject);
  });
}

async function ensureLibffmpeg(libffmpegPath, libffmpegUrl, quiet) {
  if (fs.existsSync(libffmpegPath)) {
    return libffmpegPath;
  }
  log(`下载 libffmpeg.js: ${libffmpegUrl}`, quiet);
  await downloadFile(libffmpegUrl, libffmpegPath);
  return libffmpegPath;
}

async function loadLibffmpeg(libffmpegPath) {
  global.self = global;
  global.performance = global.performance || { now: () => Date.now() };
  global.self.location = { href: `file://${libffmpegPath}` };
  global.document = {
    title: "",
    currentScript: { src: `file://${libffmpegPath}` },
  };

  let readyResolve;
  const ready = new Promise((resolve) => {
    readyResolve = resolve;
  });

  global.Module = {
    onRuntimeInitialized() {
      readyResolve();
    },
    onAbort(error) {
      throw new Error(String(error));
    },
  };

  const source = fs.readFileSync(libffmpegPath, "utf8");
  vm.runInThisContext(source, { filename: libffmpegPath });
  await ready;
  return global.Module;
}

function transHeapBuffer(module, ptr, length) {
  return Buffer.from(module.HEAPU8.subarray(ptr, ptr + length));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class FlvInputBuffer {
  constructor(maxTagBytes = 4 * 1024 * 1024) {
    this.pending = Buffer.alloc(0);
    this.headerRead = false;
    this.maxTagBytes = maxTagBytes;
    this.hasAudio = false;
    this.hasVideo = false;
    this.audioReady = false;
    this.videoReady = false;
  }

  push(chunk) {
    this.pending = Buffer.concat([this.pending, chunk]);
    const packets = [];
    if (!this.headerRead) {
      if (this.pending.length < 9) return packets;
      if (this.pending.toString("ascii", 0, 3) !== "FLV") throw new Error("上游不是 FLV 流");
      const headerBytes = this.pending.readUInt32BE(5) + 4;
      if (headerBytes < 13 || headerBytes > this.maxTagBytes) throw new Error("FLV 文件头长度无效");
      if (this.pending.length < headerBytes) return packets;
      this.hasAudio = Boolean(this.pending[4] & 4);
      this.hasVideo = Boolean(this.pending[4] & 1);
      packets.push(this.pending.subarray(0, headerBytes));
      this.pending = this.pending.subarray(headerBytes);
      this.headerRead = true;
    }
    while (this.pending.length >= 11) {
      const size = this.pending.readUIntBE(1, 3);
      const total = 11 + size + 4;
      if (total > this.maxTagBytes) throw new Error("FLV tag 超过解码缓存上限");
      if (this.pending.length < total) break;
      const packet = this.pending.subarray(0, total);
      if (packet.readUInt32BE(total - 4) !== total - 4) throw new Error("FLV tag 长度校验失败");
      const type = packet[0] & 31;
      if (type === 8 && size > 1) this.audioReady = true;
      // AVC/HEVC sequence header 后仍需完整视频包，避免 open 成功但尺寸为 0。
      if (type === 9 && size > 5 && packet[12] === 1) this.videoReady = true;
      packets.push(packet);
      this.pending = this.pending.subarray(total);
    }
    return packets;
  }

  ready() {
    return this.headerRead && (!this.hasAudio || this.audioReady) && (!this.hasVideo || this.videoReady);
  }
}

class FfmpegTsMuxer {
  constructor({
    fps,
    width,
    height,
    audioChannels,
    audioSampleRate,
    audioSampleFormat,
    outputPath,
    outputFormat,
    segmentSeconds,
    segmentStrftime,
    quiet,
    maxPendingVideoBytes,
    maxPendingAudioBytes,
    ffmpegThreads,
  }) {
    const hasAudio = Boolean(audioChannels && audioSampleRate && audioSampleFormat);
    const audioInputArgs = hasAudio
      ? [
          "-f",
          audioSampleFormat,
          "-ar",
          String(audioSampleRate),
          "-ac",
          String(audioChannels),
          "-probesize", "32",
          "-analyzeduration", "1",
          "-i",
          "pipe:3",
        ]
      : [];
    const mapArgs = hasAudio ? ["-map", "0:v:0", "-map", "1:a:0"] : ["-map", "0:v:0"];
    const audioOutputArgs = hasAudio
      ? [
          "-c:a",
          "aac",
          "-b:a",
          "48k",
          "-ac",
          String(audioChannels),
          "-ar",
          String(audioSampleRate),
        ]
      : ["-an"];
    const normalizedOutputFormat = outputFormat === "mp4" ? "mp4" : "mpegts";
    const normalizedSegmentSeconds = Math.max(0, Number(segmentSeconds || 0));
    const outputArgs = normalizedSegmentSeconds > 0
      ? [
          "-f",
          "segment",
          "-segment_time",
          String(normalizedSegmentSeconds),
          "-reset_timestamps",
          "1",
          "-segment_start_number",
          "1",
          "-segment_format",
          "mp4",
          "-segment_format_options",
          "movflags=+faststart",
          ...(segmentStrftime ? ["-strftime", "1"] : []),
        ]
      : normalizedOutputFormat === "mp4"
        ? [
          "-movflags",
          "frag_keyframe+empty_moov+default_base_moof+omit_tfhd_offset",
          "-frag_duration",
          "250000",
          "-f",
          "mp4",
          ]
        : ["-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts"];
    const x264Params = normalizedOutputFormat === "mpegts"
      ? "rc-lookahead=0:sync-lookahead=0:repeat-headers=1"
      : "rc-lookahead=0:sync-lookahead=0";
    const ffmpegArgs = [
      "-loglevel",
      "error",
      "-fflags",
      "+genpts",
      "-flags",
      "low_delay",
      "-f",
      "rawvideo",
      "-pix_fmt",
      "yuv420p",
      "-video_size",
      `${width}x${height}`,
      "-framerate",
      String(fps),
      "-probesize", "32",
      "-analyzeduration", "1",
      "-i",
      "pipe:0",
      ...audioInputArgs,
      ...mapArgs,
      "-threads",
      String(Math.max(1, Number(ffmpegThreads || 1))),
      "-c:v",
      "libx264",
      "-profile:v",
      "baseline",
      "-level",
      "3.1",
      "-preset",
      "veryfast",
      "-tune",
      "zerolatency",
      "-x264-params",
      x264Params,
      "-g",
      String(Math.max(1, Math.round(fps))),
      "-keyint_min",
      String(Math.max(1, fps)),
      "-bf",
      "0",
      ...audioOutputArgs,
      "-pix_fmt",
      "yuv420p",
      "-flush_packets", "1",
      "-max_interleave_delta", "100000",
      ...outputArgs,
      outputPath || "pipe:1",
    ];

    this.process = spawn("ffmpeg", ffmpegArgs, {
      stdio: ["pipe", outputPath ? "ignore" : "pipe", quiet ? "ignore" : "inherit", hasAudio ? "pipe" : "ignore"],
    });
    this.videoIn = this.process.stdin;
    this.audioIn = hasAudio ? this.process.stdio[3] : null;
    this.stdout = outputPath ? null : this.process.stdout;
    this.videoQueue = [];
    this.audioQueue = [];
    this.videoQueuedBytes = 0;
    this.audioQueuedBytes = 0;
    this.maxPendingVideoBytes = Math.max(1024 * 1024, Number(maxPendingVideoBytes || 8 * 1024 * 1024));
    this.maxPendingAudioBytes = Math.max(256 * 1024, Number(maxPendingAudioBytes || 2 * 1024 * 1024));
    this.videoFrameBytes = width * height * 3 / 2;
    this.audioFrameBytes = 0;
    this.videoDraining = false;
    this.audioDraining = false;
    this.closed = false;
    this.closing = false;
    this.lastError = null;
    for (const input of [this.videoIn, this.audioIn].filter(Boolean)) {
      input.on("error", (error) => { this.lastError = error; });
    }
    this.exitPromise = new Promise((resolve) => {
      this.resolveExit = resolve;
    });

    const onProcessExit = (error) => {
      this.lastError = error || this.lastError || new Error("FFmpeg 已退出");
      this.closed = true;
      this.videoQueue.length = 0;
      this.audioQueue.length = 0;
      this.videoQueuedBytes = 0;
      this.audioQueuedBytes = 0;
      if (this.resolveExit) {
        this.resolveExit(error || null);
        this.resolveExit = null;
      }
    };

    this.process.once("error", (error) => onProcessExit(error));
    this.process.once("exit", (code, signal) => {
      if (code === 0) {
        onProcessExit(null);
        return;
      }
      const reason = signal ? `signal=${signal}` : `code=${code}`;
      onProcessExit(new Error(`FFmpeg 异常退出 (${reason})`));
    });
  }

  getError() {
    return this.lastError;
  }

  isBackpressured() {
    return (
      (this.videoQueuedBytes > 0 && this.videoQueuedBytes + this.videoFrameBytes > this.maxPendingVideoBytes) ||
      (this.audioQueuedBytes > 0 && this.audioQueuedBytes + this.audioFrameBytes > this.maxPendingAudioBytes)
    );
  }

  enqueueVideo(frameBuffer) {
    return this.enqueueFrame("video", frameBuffer);
  }

  enqueueAudio(frameBuffer) {
    this.audioFrameBytes = Math.max(this.audioFrameBytes, frameBuffer.length);
    return this.enqueueFrame("audio", frameBuffer);
  }

  enqueueFrame(kind, frameBuffer) {
    if (!frameBuffer || frameBuffer.length === 0) {
      return true;
    }
    if (this.closed || this.closing) {
      return false;
    }

    const queueKey = kind === "video" ? "videoQueue" : "audioQueue";
    const bytesKey = kind === "video" ? "videoQueuedBytes" : "audioQueuedBytes";
    const limit = kind === "video" ? this.maxPendingVideoBytes : this.maxPendingAudioBytes;

    if (this[bytesKey] + frameBuffer.length > limit) {
      return false;
    }

    this[queueKey].push(frameBuffer);
    this[bytesKey] += frameBuffer.length;
    this.drainQueue(kind);
    return true;
  }

  drainQueue(kind) {
    const stream = kind === "video" ? this.videoIn : this.audioIn;
    const queueKey = kind === "video" ? "videoQueue" : "audioQueue";
    const bytesKey = kind === "video" ? "videoQueuedBytes" : "audioQueuedBytes";
    const drainingKey = kind === "video" ? "videoDraining" : "audioDraining";

    if (this[drainingKey]) {
      return;
    }
    this[drainingKey] = true;

    const resume = () => {
      stream.off("error", fail);
      this[drainingKey] = false;
      this.drainQueue(kind);
    };

    const fail = (error) => {
      stream.off("drain", resume);
      this.lastError = error;
      this.closed = true;
      this[drainingKey] = false;
      this[queueKey].length = 0;
      this[bytesKey] = 0;
    };

    if (!stream || !stream.writable || this.closed) {
      fail(this.lastError || new Error(`FFmpeg ${kind} 输入流不可写`));
      return;
    }

    try {
      while (this[queueKey].length > 0) {
        const frameBuffer = this[queueKey][0];
        const writable = stream.write(frameBuffer);
        this[queueKey].shift();
        this[bytesKey] -= frameBuffer.length;
        if (!writable) {
          stream.once("drain", resume);
          stream.once("error", fail);
          return;
        }
      }
      this[drainingKey] = false;
      if (this.closing && !stream.writableEnded) stream.end();
    } catch (error) {
      fail(error);
    }
  }

  close() {
    if (this.closed || this.closing) {
      return;
    }
    // EOF 必须排在所有已接受帧之后；write(false) 仅表示等待 drain，不能丢弃队列。
    this.closing = true;
    this.drainQueue("video");
    if (this.audioIn) this.drainQueue("audio");
  }

  terminate(signalName = "SIGTERM") {
    this.close();
    if (this.process && !this.process.killed) {
      this.process.kill(signalName);
    }
  }

  async waitForExit(timeoutMs = 10000) {
    let timeoutId;
    const timeout = new Promise((_, reject) => {
      timeoutId = setTimeout(() => reject(new Error("等待 FFmpeg 封装结束超时")), timeoutMs);
    });
    try {
      return await Promise.race([this.exitPromise, timeout]);
    } finally {
      clearTimeout(timeoutId);
    }
  }
}

class VideoFrameClock {
  constructor(fps) {
    if (!Number.isFinite(fps) || fps <= 0 || fps > 120) throw new Error("输出 fps 必须在 0-120 之间");
    this.fps = fps;
    this.origin = null;
    this.nextFrame = 0;
    this.previous = null;
    this.lastTimestamp = null;
  }

  sample(frame, timestamp) {
    if (!Number.isFinite(timestamp)) throw new Error("视频时间戳无效");
    if (this.origin === null) this.origin = timestamp;
    if (this.lastTimestamp !== null && (timestamp < this.lastTimestamp || timestamp - this.lastTimestamp > 2000)) {
      throw new Error("视频时间戳不连续，停止录制以避免错误时间线");
    }
    const output = [];
    // FLV 时间戳只有毫秒精度，允许 1 ms 量化误差，避免 83 ms 被错判为尚未到 1/12 秒。
    const lastIndex = Math.floor((timestamp - this.origin + 1) * this.fps / 1000);
    while (this.nextFrame <= lastIndex) {
      const sampleTime = this.origin + this.nextFrame * 1000 / this.fps;
      output.push(this.previous && sampleTime < timestamp - .001 ? this.previous : frame);
      this.nextFrame += 1;
    }
    this.previous = frame;
    this.lastTimestamp = timestamp;
    return output;
  }
}

class CameraWasmDecoder {
  constructor(module, options) {
    this.module = module;
    this.options = options;
    this.cacheBuffer = 0;
    this.infoPtr = 0;
    this.queue = [];
    this.inputSize = 0;
    this.opened = false;
    this.opening = false;
    this.ended = false;
    this.videoFrames = 0;
    this.lastProgressAt = Date.now();
    this.audioFrames = 0;
    this.pendingInitialAudio = [];
    this.pendingInitialAudioBytes = 0;
    this.ffmpegMuxer = null;
    this.keyType = Number(options.keyType || 0);
    this.minBufferSize = Number(options.minBufferSize || 1);
    this.nextOpenSize = this.minBufferSize;
    this.startupChunks = [];
    this.decoderMemorySize = Number(options.decoderMemorySize || 5242880);
    this.chunkSize = Number(options.chunkSize || 524288);
    this.fps = Number(options.fps || 12);
    this.videoClock = new VideoFrameClock(this.fps);
    this.pendingVideoFrames = [];
    this.maxFrames = options.maxFrames ? Number(options.maxFrames) : 0;
    this.outputPath = options.outputPath || "";
    this.quiet = Boolean(options.quiet);
    this.maxPendingVideoBytes = Number(options.maxPendingVideoBytes || 8 * 1024 * 1024);
    this.maxPendingAudioBytes = Number(options.maxPendingAudioBytes || 2 * 1024 * 1024);
    this.maxPendingInputBytes = Number(options.maxPendingInputBytes || 4 * this.chunkSize);
    this.ffmpegThreads = Number(options.ffmpegThreads || 1);
    this.queuedInputBytes = 0;
    this.audioSampleFormat = null;
    this.audioChannels = 0;
    this.audioSampleRate = 0;
    this.videoWidth = 0;
    this.videoHeight = 0;
    this.droppedVideoFrames = 0;
    this.droppedAudioFrames = 0;
    this.flvInput = new FlvInputBuffer(Math.min(4 * 1024 * 1024, this.decoderMemorySize - this.chunkSize));
  }

  async init() {
    const ret = this.module._initDecoder(this.decoderMemorySize, 0, 0, this.quiet ? 0 : 1, 0, 1);
    if (ret !== 0) {
      throw new Error(`_initDecoder 失败: ${ret}`);
    }
    this.cacheBuffer = this.module._malloc(this.chunkSize);
    this.infoPtr = this.module._malloc(28);
    this.keyPtr = this.options.playKey ? this.module.allocateUTF8(this.options.playKey) : 0;
    this.relayPtr = this.options.relaySig ? this.module.allocateUTF8(this.options.relaySig) : 0;
    this.videoCb = this.module.addFunction((ptr, size, ts, width, height) => {
      if (!this.ffmpegMuxer) {
        this.ffmpegMuxer = new FfmpegTsMuxer({
          fps: this.fps,
          width,
          height,
          audioChannels: this.audioChannels,
          audioSampleRate: this.audioSampleRate,
          audioSampleFormat: this.audioSampleFormat,
          outputPath: this.outputPath,
          outputFormat: this.options.outputFormat,
          segmentSeconds: this.options.segmentSeconds,
          segmentStrftime: this.options.segmentStrftime,
          quiet: this.quiet,
          maxPendingVideoBytes: this.maxPendingVideoBytes,
          maxPendingAudioBytes: this.maxPendingAudioBytes,
          ffmpegThreads: this.ffmpegThreads,
        });
        if (!this.outputPath && this.ffmpegMuxer.stdout) {
          this.ffmpegMuxer.stdout.pipe(process.stdout);
        }
        log(`FFmpeg muxer started: ${width}x${height} @ ${this.fps}fps`, this.quiet);
        for (const audio of this.pendingInitialAudio) {
          if (!this.ffmpegMuxer.enqueueAudio(audio)) throw new Error("启动音频缓存无法写入 FFmpeg");
        }
        this.pendingInitialAudio.length = 0;
        this.pendingInitialAudioBytes = 0;
      }
      this.videoFrames += 1;
      this.lastProgressAt = Date.now();
      const frame = transHeapBuffer(this.module, ptr, size);
      // rawvideo 不携带时间戳：按源 PTS 采样到输出 fps，否则 25 fps 会被当成 12 fps，
      // 视频时间线跑到音频前面，最终双管道互相等待而永久背压。
      this.pendingVideoFrames.push(...this.videoClock.sample(frame, ts));
      this.flushVideoFrames();
      if (this.maxFrames && this.videoFrames >= this.maxFrames) {
        this.ended = true;
      }
      if (this.videoFrames <= 3) {
        log(`video frame #${this.videoFrames} ts=${ts} size=${size}`, this.quiet);
      }
    });
    this.audioCb = this.module.addFunction((ptr, size, ts, duration) => {
      this.audioFrames += 1;
      this.lastProgressAt = Date.now();
      if (!this.ffmpegMuxer && this.audioSampleFormat) {
        if (this.pendingInitialAudioBytes + size > this.maxPendingAudioBytes) throw new Error("等待首帧时音频缓存超过上限");
        this.pendingInitialAudio.push(transHeapBuffer(this.module, ptr, size));
        this.pendingInitialAudioBytes += size;
      }
      if (this.ffmpegMuxer && this.audioSampleFormat) {
        const frame = transHeapBuffer(this.module, ptr, size);
        if (!this.ffmpegMuxer.enqueueAudio(frame)) {
          const error = this.ffmpegMuxer.getError();
          if (error) {
            throw error;
          }
          throw new Error("音频待写入缓存不足，停止管线以避免录像静默丢帧");
        }
      }
      if (this.audioFrames <= 3) {
        log(`audio frame #${this.audioFrames} ts=${ts} duration=${duration} size=${size}`, this.quiet);
      }
    });
    this.seekCb = this.module.addFunction(() => {});
  }

  enqueue(chunk) {
    if (chunk && chunk.length) {
      // WASM 会消耗不完整 tag，之后无法恢复该帧。网络块必须先拼成完整 FLV tag。
      for (const frame of this.flvInput.push(chunk)) {
        this.queue.push(frame);
        this.queuedInputBytes += frame.length;
      }
    }
  }

  isInputBackpressured() {
    return this.queuedInputBytes >= this.maxPendingInputBytes;
  }

  isBackpressured() {
    return this.isInputBackpressured() || Boolean(this.ffmpegMuxer && this.ffmpegMuxer.isBackpressured());
  }

  flushInput() {
    while (this.queue.length > 0) {
      const pending = this.queue[0];
      const chunk = pending.subarray(0, this.chunkSize);
      this.module.HEAPU8.set(chunk, this.cacheBuffer);
      const wrote = this.module._sendData(this.cacheBuffer, chunk.length);
      if (wrote < 0) {
        throw new Error(`_sendData 失败: ${wrote}`);
      }
      if (wrote === 0) {
        break;
      }
      this.inputSize += wrote;
      if (!this.opened) {
        if (this.inputSize > this.decoderMemorySize - this.chunkSize) {
          throw new Error("解码器启动数据超过内存上限");
        }
        this.startupChunks.push(Buffer.from(chunk.subarray(0, wrote)));
      }
      if (wrote === pending.length) {
        this.queuedInputBytes -= wrote;
        this.queue.shift();
      } else {
        this.queuedInputBytes -= wrote;
        this.queue[0] = pending.subarray(wrote);
        if (wrote < chunk.length) break;
      }
    }
  }

  maybeOpen() {
    if (this.opened || this.opening || this.inputSize < this.nextOpenSize || !this.flvInput.ready()) {
      return;
    }
    this.opening = true;
    const ret = this.module._openDecoder(
      this.infoPtr,
      7,
      this.videoCb,
      this.audioCb,
      this.seekCb,
      this.keyPtr,
      this.keyType,
      this.relayPtr,
      0
    );
    this.opening = false;
    if (ret === 8 && this.inputSize < 512 * 1024) {
      // 实测碎片化 FLV 头使 open 返回 8，且之后 sendData 会返回 -1。
      // 重建原生解码器并重放启动数据，不能在已失败的实例上直接继续写。
      this.module._uninitDecoder();
      const reset = this.module._initDecoder(this.decoderMemorySize, 0, 0, this.quiet ? 0 : 1, 0, 1);
      if (reset !== 0) throw new Error(`重建解码器失败: ${reset}`);
      for (const chunk of this.startupChunks) {
        this.module.HEAPU8.set(chunk, this.cacheBuffer);
        if (this.module._sendData(this.cacheBuffer, chunk.length) !== chunk.length) {
          throw new Error("重放解码器启动数据失败");
        }
      }
      this.nextOpenSize = Math.min(512 * 1024, Math.max(this.inputSize + 16 * 1024, this.inputSize * 2));
      return;
    }
    if (ret !== 0) {
      throw new Error(`_openDecoder 失败: ${ret}`);
    }
    this.opened = true;
    this.startupChunks.length = 0;
    const info = Array.from(this.module.HEAP32.subarray(this.infoPtr >> 2, (this.infoPtr >> 2) + 7));
    this.videoWidth = info[2];
    this.videoHeight = info[3];
    this.audioSampleFormat = this.mapAudioSampleFormat(info[4]);
    this.audioChannels = info[5];
    this.audioSampleRate = info[6];
    log(`Decoder opened: duration=${info[0]}s width=${info[2]} height=${info[3]}`, this.quiet);
  }

  mapAudioSampleFormat(sampleFmt) {
    const formatMap = {
      0: "u8",
      1: "s16le",
      2: "s32le",
      3: "f32le",
      5: "u8",
      6: "s16le",
      7: "s32le",
      8: "f32le",
      9: "f64le",
      10: "s64le",
      11: "s64le",
    };
    return formatMap[sampleFmt] || null;
  }

  flushVideoFrames() {
    while (this.pendingVideoFrames.length) {
      const frame = this.pendingVideoFrames[0];
      if (!this.ffmpegMuxer.enqueueVideo(frame)) {
        if (this.ffmpegMuxer.getError()) throw this.ffmpegMuxer.getError();
        if (frame.length > this.ffmpegMuxer.maxPendingVideoBytes) throw new Error("单帧超过视频缓存上限");
        return false;
      }
      this.pendingVideoFrames.shift();
      this.lastProgressAt = Date.now();
    }
    return true;
  }

  pumpDecode(maxIterations = 256) {
    if (!this.opened || this.ended) {
      return this.ended ? "ended" : "need-input";
    }
    for (let i = 0; i < maxIterations; i += 1) {
      if (this.ended) return "ended";
      if (this.ffmpegMuxer) {
        const muxerError = this.ffmpegMuxer.getError();
        if (muxerError) {
          throw muxerError;
        }
        if (!this.flushVideoFrames() || this.ffmpegMuxer.isBackpressured()) {
          return "backpressure";
        }
      }
      const ret = this.module._decodeOnePacket();
      if (ret === 0) {
        continue;
      }
      if (ret === 9) {
        return "need-input";
      }
      if (ret === 7) {
        this.ended = true;
        return "ended";
      }
      throw new Error(`_decodeOnePacket 失败: ${ret}`);
    }
    return "progress";
  }

  finish() {
    this.ended = true;
    if (this.ffmpegMuxer) {
      this.ffmpegMuxer.close();
    }
  }

  async waitForMuxerExit(timeoutMs = 10000) {
    if (this.ffmpegMuxer) {
      const error = await this.ffmpegMuxer.waitForExit(timeoutMs);
      if (error) throw error;
    }
  }
}

async function* chunkFromFile(filePath, chunkSize) {
  const handle = await fs.promises.open(filePath, "r");
  try {
    let position = 0;
    while (true) {
      const buffer = Buffer.alloc(chunkSize);
      const { bytesRead } = await handle.read(buffer, 0, chunkSize, position);
      if (!bytesRead) {
        break;
      }
      position += bytesRead;
      yield buffer.subarray(0, bytesRead);
    }
  } finally {
    await handle.close();
  }
}

async function* chunkFromFetch(url, chunkSize, quiet, signal) {
  log("fetch stream started", quiet);
  const response = await fetch(url, {
    headers: {
      Referer: "https://my.jia.360.cn/",
      "User-Agent": "Mozilla/5.0",
      Accept: "*/*",
    },
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`拉取视频流失败: HTTP ${response.status}`);
  }
  for await (const rawChunk of response.body) {
    const chunk = Buffer.from(rawChunk);
    if (chunk.length <= chunkSize) {
      yield chunk;
      continue;
    }
    for (let offset = 0; offset < chunk.length; offset += chunkSize) {
      yield chunk.subarray(offset, Math.min(offset + chunkSize, chunk.length));
    }
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (!args.url && !args["input-file"]) {
    throw new Error("需要传入 --url 或 --input-file");
  }
  const quiet = Boolean(args.quiet);
  const libffmpegUrl = args["libffmpeg-url"] || process.env.CAMERA_LIBFFMPEG_URL || DEFAULT_LIBFFMPEG_URL;
  const libffmpegPath = path.resolve(args["libffmpeg-path"] || DEFAULT_LIBFFMPEG_PATH);
  const chunkSize = Number(args["network-chunk-size"] || 64 * 1024);

  await ensureLibffmpeg(libffmpegPath, libffmpegUrl, quiet);
  const module = await loadLibffmpeg(libffmpegPath);
  const decoder = new CameraWasmDecoder(module, {
    playKey: args["play-key"],
    relaySig: args["relay-sig"] || "",
    keyType: args["key-type"] || 0,
    fps: args.fps || 12,
    outputPath: args.output || "",
    outputFormat: args["output-format"] || "mpegts",
    segmentSeconds: args["segment-seconds"] || 0,
    segmentStrftime: Boolean(args["segment-strftime"]),
    maxFrames: args["max-frames"] || 0,
    minBufferSize: args["min-decoder-buffer-size"] || 1,
    chunkSize,
    maxPendingVideoBytes: args["max-pending-video-bytes"] || 8 * 1024 * 1024,
    maxPendingAudioBytes: args["max-pending-audio-bytes"] || 2 * 1024 * 1024,
    maxPendingInputBytes: args["max-pending-input-bytes"] || 2 * 1024 * 1024,
    ffmpegThreads: args["ffmpeg-threads"] || 1,
    quiet,
  });
  activeDecoder = decoder;
  await decoder.init();

  if (args["control-stdin"]) {
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", handleControlInput);
    process.stdin.resume();
  }

  activeAbortController = new AbortController();
  const stallTimeoutMs = Number(args["stall-timeout-ms"] || 30000);
  let stallError = null;
  const checkProgress = () => {
    if (!stopRequested && Date.now() - decoder.lastProgressAt > stallTimeoutMs) {
      stallError = stallError || new Error(`解密/编码管线超过 ${stallTimeoutMs}ms 无进展（video=${decoder.videoFrames}, audio=${decoder.audioFrames}），停止以避免空转录制`);
      activeAbortController?.abort();
    }
    if (stallError) throw stallError;
  };
  const progressTimer = setInterval(() => {
    try { checkProgress(); } catch (_) { /* 主循环通过 abort 或下一次检查接收错误。 */ }
  }, Math.min(1000, stallTimeoutMs));
  progressTimer.unref();

  const source = args["input-file"]
    ? chunkFromFile(path.resolve(args["input-file"]), chunkSize)
    : chunkFromFetch(args.url, chunkSize, quiet, activeAbortController.signal);

  try {
    for await (const chunk of source) {
      while (decoder.isBackpressured() && !decoder.ended) {
        checkProgress();
        decoder.flushInput();
        decoder.maybeOpen();
        decoder.pumpDecode(32);
        await sleep(10);
      }
      if (decoder.ended) {
        break;
      }
      decoder.enqueue(chunk);
      decoder.flushInput();
      decoder.maybeOpen();
      decoder.pumpDecode();
      if (decoder.ended) {
        break;
      }
    }
  } catch (error) {
    if (stallError) throw stallError;
    if (!stopRequested || error.name !== "AbortError") {
      throw error;
    }
  }

  // JS 输入队列为空不代表 WASM 内部已排空。必须持续解码至原生层要求新输入。
  while (!decoder.ended) {
    checkProgress();
    decoder.flushInput();
    decoder.maybeOpen();
    const state = decoder.pumpDecode(32);
    if (state === "need-input" && decoder.queue.length === 0) break;
    await sleep(10);
  }

  while (!decoder.flushVideoFrames()) {
    checkProgress();
    await sleep(10);
  }
  decoder.finish();
  await decoder.waitForMuxerExit(8000);
  clearInterval(progressTimer);
  if (!stopRequested && !decoder.videoFrames) throw new Error("输入结束但未解码出视频帧");
  if (!stopRequested && decoder.flvInput.pending.length) throw new Error("输入结束时存在不完整 FLV tag");
  log(
    `decoder finished: videoFrames=${decoder.videoFrames} audioFrames=${decoder.audioFrames} droppedVideoFrames=${decoder.droppedVideoFrames} droppedAudioFrames=${decoder.droppedAudioFrames}`,
    quiet
  );
  cleanupControlInput();
  activeAbortController = null;
  activeDecoder = null;
}

let activeDecoder = null;
let activeAbortController = null;
let stopRequested = false;
let controlInputBuffer = "";

function requestGracefulStop() {
  if (stopRequested) {
    return;
  }
  stopRequested = true;
  // 主循环收到 abort 后排空 WASM 和帧队列，再向编码器发送 EOF。
  if (activeAbortController) {
    activeAbortController.abort();
  }
}

function handleControlInput(rawText) {
  controlInputBuffer += String(rawText);
  const lines = controlInputBuffer.split(/\r?\n/);
  controlInputBuffer = lines.pop() || "";
  for (const line of lines) {
    if (line.trim().toLowerCase() === "stop") {
      requestGracefulStop();
    }
  }
}

function cleanupControlInput() {
  process.stdin.off("data", handleControlInput);
  process.stdin.pause();
}

async function shutdown(signalName) {
  requestGracefulStop();
  let exitCode = 0;
  try {
    if (activeDecoder) {
      await activeDecoder.waitForMuxerExit(8000);
    }
  } catch (error) {
    exitCode = 1;
    if (activeDecoder && activeDecoder.ffmpegMuxer) {
      activeDecoder.ffmpegMuxer.terminate(signalName);
    }
  } finally {
    cleanupControlInput();
    process.exit(exitCode);
  }
}

if (require.main === module) {
  process.once("SIGINT", () => void shutdown("SIGINT"));
  process.once("SIGTERM", () => void shutdown("SIGTERM"));
  main().catch((error) => {
    if (activeDecoder && activeDecoder.ffmpegMuxer) {
      activeDecoder.ffmpegMuxer.terminate("SIGTERM");
    }
    activeDecoder = null;
    process.stderr.write(`${error.stack || error.message}\n`);
    process.exit(1);
  });
}

module.exports = { CameraWasmDecoder, FfmpegTsMuxer, FlvInputBuffer, VideoFrameClock, loadLibffmpeg };
