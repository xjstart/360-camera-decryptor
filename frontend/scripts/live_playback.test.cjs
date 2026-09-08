const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
// 只取纯播放控制函数，不执行页面初始化。
const source = fs.readFileSync(require('node:path').join(__dirname, 'index.js'), 'utf8');
const functionSource = source.slice(source.indexOf('function chaseBackendLiveEdge('), source.indexOf('// ==================== 测试配置'));
const context = vm.createContext({});
vm.runInContext(functionSource, context);
const ranges = (start, end) => ({ length: 1, start: () => start, end: () => end });
const video = () => ({ paused: false, ended: false, seeking: false, buffered: ranges(0, 10), seekable: ranges(0, 10), currentTime: 5, playbackRate: 1 });

test('live catch-up stays inside received and seekable media', () => {
    const media = video();
    context.chaseBackendLiveEdge(media);
    assert.equal(media.currentTime, 9.5);
    assert.equal(media.playbackRate, 1);
});
test('nonseekable live MP4 uses gentle catch-up and resets near live edge', () => {
    const media = video(); media.seekable = { length: 0 };
    context.chaseBackendLiveEdge(media);
    assert.equal(media.currentTime, 5);
    assert.equal(media.playbackRate, 1.05);
    media.currentTime = 9.5;
    context.chaseBackendLiveEdge(media);
    assert.equal(media.playbackRate, 1);
});
test('paused or seeking playback is not repositioned', () => {
    for (const state of ['paused', 'seeking']) {
        const media = video(); media[state] = true;
        context.chaseBackendLiveEdge(media);
        assert.equal(media.currentTime, 5);
    }
});
