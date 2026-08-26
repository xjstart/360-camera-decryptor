// ==================== 全局变量 ====================
let apiResponse = null;
let currentPlayer = null;
let currentConfigId = null;
let go2rtcConfig = null;
let backendStreamConfigId = null;
let backendStreamSn = '';
let backendStreamAbortController = null;
let backendStreamObjectUrl = '';
let recordingPollTimer = null;
let recordingPollSn = '';
let recordingActiveState = 'idle';

function getDefaultBackendBaseUrl() {
    if (window.location.protocol === 'http:' || window.location.protocol === 'https:') {
        return window.location.origin;
    }
    return 'http://127.0.0.1:5000';
}

function getBackendBaseUrl() {
    return document.getElementById('backend-base-url').value.trim().replace(/\/$/, '');
}

function buildApiUrl(path) {
    return `${getBackendBaseUrl()}${path}`;
}

function slugifyGo2rtcName(name, sn = '') {
    const lowerName = (name || '').toLowerCase();
    const normalized = lowerName
        .replace(/[^a-z0-9]+/g, '_')
        .replace(/^_+|_+$/g, '');
    const snSuffix = (sn || 'unknown').toLowerCase().slice(-6);
    return normalized ? `${normalized}_${snSuffix}` : `camera_${snSuffix}`;
}

function buildLocalGo2rtcConfig(sn) {
    const cameraName = (apiResponse && (apiResponse.camera_name || apiResponse.name)) || '';
    const streamName = slugifyGo2rtcName(cameraName, sn);
    const mode = document.getElementById('go2rtc-mode').value || 'decrypted';
    const configId = currentConfigId !== null ? currentConfigId : 0;
    const isDecryptedMode = mode === 'decrypted';
    const sourceUrl = buildApiUrl(isDecryptedMode ? `/api/decrypted-stream/${configId}/${encodeURIComponent(sn)}` : `/api/stream/${encodeURIComponent(sn)}`);
    const go2rtcSource = `${sourceUrl}#input=${isDecryptedMode ? 'mpegts' : 'flv'}`;
    const yaml = `streams:\n  ${streamName}:\n  - ${go2rtcSource}\n`;

    return {
        count: 1,
        mode,
        note: isDecryptedMode
            ? '后端未提供 /api/go2rtc/config，当前为前端本地生成的服务端解密配置；请确认后端已升级且支持 /api/decrypted-stream/<sn>。'
            : '后端未提供 /api/go2rtc/config，当前为前端本地生成的原始流代理配置。',
        public_base_url: getBackendBaseUrl(),
        streams: [
            {
                name: cameraName,
                sn: sn,
                stream_name: streamName,
                source_url: sourceUrl,
                go2rtc_source: go2rtcSource,
                mode
            }
        ],
        yaml
    };
}

function applyGo2rtcConfig(result, sourceLabel = '后端接口') {
    go2rtcConfig = result;
    const stream = (result.streams || [])[0] || {};
    document.getElementById('go2rtc-yaml').value = result.yaml || '';
    document.getElementById('go2rtc-stream-name').textContent = stream.stream_name || '-';
    document.getElementById('go2rtc-source-url').textContent = stream.go2rtc_source || stream.ffmpeg_source || '-';
    document.getElementById('go2rtc-section').classList.add('visible');
    log(`go2rtc 配置已生成 (${sourceLabel})`, 'success');
}

// ==================== 日志函数 ====================
function log(message, type = 'info') {
    const logContainer = document.getElementById('log-container');
    const time = new Date().toLocaleTimeString();
    const entry = document.createElement('div');
    entry.className = `log-entry log-${type}`;
    entry.textContent = `[${time}] ${message}`;
    logContainer.appendChild(entry);
    logContainer.scrollTop = logContainer.scrollHeight;
    console.log(`[${type.toUpperCase()}]`, message);
}

function syncCameraSelection() {
    const select = document.getElementById('camera-select');
    const snInput = document.getElementById('camera-sn-input');
    if (select.value) {
        snInput.value = select.value;
        localStorage.setItem('cameraDecryptorSelectedSn', select.value);
        refreshRecordingStatus(select.value);
    } else {
        snInput.value = '';
        refreshRecordingStatus('');
    }
}

async function loadCameraList() {
    const select = document.getElementById('camera-select');
    select.innerHTML = '<option value="">摄像机列表加载中...</option>';

    try {
        const response = await fetch(buildApiUrl('/api/cameras'));
        const result = await response.json();

        if (!response.ok || result.error) {
            throw new Error(result.error || '加载摄像机列表失败');
        }

        const cameras = result.cameras || [];
        if (!cameras.length) {
            select.innerHTML = '<option value="">配置中没有可用摄像机</option>';
            log('后端已连接，但配置中没有可用摄像机', 'warning');
            return;
        }

        select.innerHTML = '<option value="">请选择摄像机</option>';
        cameras.forEach((camera) => {
            const option = document.createElement('option');
            option.value = camera.sn;
            option.textContent = `${camera.name || camera.sn} (${camera.api_version || 'v2'})`;
            select.appendChild(option);
        });

        const savedSn = localStorage.getItem('cameraDecryptorSelectedSn') || '';
        if (savedSn && cameras.some((camera) => camera.sn === savedSn)) {
            select.value = savedSn;
            document.getElementById('camera-sn-input').value = savedSn;
            refreshRecordingStatus(savedSn);
        } else {
            const defaultCamera = cameras[0];
            select.value = defaultCamera.sn;
            document.getElementById('camera-sn-input').value = defaultCamera.sn;
            localStorage.setItem('cameraDecryptorSelectedSn', defaultCamera.sn);
            refreshRecordingStatus(defaultCamera.sn);
        }

        log(`已加载 ${cameras.length} 个摄像机`, 'success');
    } catch (error) {
        select.innerHTML = '<option value="">加载失败</option>';
        log(`加载摄像机列表失败: ${error.message}`, 'error');
    }
}

async function fetchPlayInfo() {
    const sn = document.getElementById('camera-sn-input').value.trim();
    if (!sn) {
        log('请先输入摄像机 SN', 'error');
        alert('请先输入摄像机 SN');
        return;
    }

    log(`正在从后端获取播放信息: ${sn}`, 'info');

    try {
        const response = await fetch(buildApiUrl(`/api/play-info?sn=${encodeURIComponent(sn)}`));
        const result = await response.json();

        if (!response.ok || result.errorCode !== 0) {
            throw new Error(result.errorMsg || result.error || '获取播放信息失败');
        }

        document.getElementById('json-input').value = JSON.stringify(result, null, 4);
        applyApiResponse(result, '后端接口');
        await fetchGo2rtcConfig(sn);
    } catch (error) {
        log(`获取播放信息失败: ${error.message}`, 'error');
        alert(`获取播放信息失败: ${error.message}`);
    }
}

// ==================== JSON解析 ====================
function parseJson() {
    const jsonInput = document.getElementById('json-input').value.trim();

    if (!jsonInput) {
        log('请输入JSON数据', 'error');
        alert('请输入JSON数据');
        return;
    }

    try {
        const parsed = JSON.parse(jsonInput);
        applyApiResponse(parsed, '手动 JSON');
    } catch (e) {
        log(`JSON解析失败: ${e.message}`, 'error');
        alert(`JSON解析失败: ${e.message}`);
    }
}

function applyApiResponse(payload, sourceLabel) {
    apiResponse = payload;
    log(`${sourceLabel} 数据解析成功`, 'success');

    if (apiResponse.errorCode !== undefined) {
        if (apiResponse.errorCode !== 0) {
            log(`API返回错误: ${apiResponse.errorMsg || '未知错误'}`, 'error');
            alert(`API返回错误: ${apiResponse.errorMsg || '未知错误'}`);
        } else {
            log('API返回成功', 'success');
        }
    }

    displayVideoInfo();
    document.getElementById('go2rtc-section').classList.add('visible');
    document.getElementById('config-section').classList.add('visible');
    document.getElementById('player-section').classList.add('visible');
    log('视频流信息已加载，请选择解密配置进行测试', 'info');
}

// ==================== 显示视频流信息 ====================
function displayVideoInfo() {
    if (!apiResponse) return;

    document.getElementById('info-flashUrl').textContent = apiResponse.flashUrl || apiResponse.sourceFlashUrl || '-';
    document.getElementById('info-relayStream').textContent = apiResponse.relayStream || '-';
    document.getElementById('info-playKey').textContent = apiResponse.playKey || '-';
    document.getElementById('info-keyLength').textContent = apiResponse.playKey ? `${apiResponse.playKey.length} 字符` : '-';
    document.getElementById('info-relay').textContent = apiResponse.relay ? apiResponse.relay.join(', ') : '-';
    document.getElementById('info-relayId').textContent = apiResponse.relayId || '-';
    document.getElementById('info-relaySig').textContent = apiResponse.relaySig || '-';
    document.getElementById('info-errorMsg').textContent = apiResponse.backendDecryptNote || apiResponse.errorMsg || '-';

    document.getElementById('video-stream-section').classList.add('visible');

    log('视频流信息已显示', 'success');
}

function getCurrentCameraSn() {
    return (apiResponse && (apiResponse.camera_sn || apiResponse.sn)) || document.getElementById('camera-sn-input').value.trim();
}

function getRecordingCameraSn() {
    const input = document.getElementById('camera-sn-input');
    return input ? input.value.trim() : '';
}

function formatRecordingTime(value) {
    if (!value) {
        return '-';
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatSegmentDuration(seconds) {
    if (seconds % 3600 === 0) {
        return `${seconds / 3600} 小时`;
    }
    if (seconds % 60 === 0) {
        return `${seconds / 60} 分钟`;
    }
    return `${seconds} 秒`;
}

async function loadRecordingSettings() {
    const pathInput = document.getElementById('recording-output-root');
    const durationInput = document.getElementById('recording-segment-seconds');
    try {
        const response = await fetch(buildApiUrl('/api/recordings/settings'), { cache: 'no-store' });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.error) {
            throw new Error(result.error || `HTTP ${response.status}`);
        }
        if (recordingActiveState !== 'recording' && recordingActiveState !== 'stopping') {
            const savedPath = localStorage.getItem('cameraDecryptorRecordingDir') || '';
            pathInput.value = savedPath || result.recording_dir || '';
            durationInput.value = result.default_segment_seconds || 1800;
        }
    } catch (error) {
        durationInput.value = 1800;
        log(`读取录像设置失败: ${error.message}`, 'warning');
    }
}

function updateRecordingConfigDisplay(configId = null) {
    const value = configId !== null ? configId : (currentConfigId !== null ? currentConfigId : 0);
    const element = document.getElementById('recording-current-config');
    if (element) {
        element.textContent = `配置 ${value}`;
    }
}

function stopRecordingPolling() {
    if (recordingPollTimer) {
        clearTimeout(recordingPollTimer);
        recordingPollTimer = null;
    }
    recordingPollSn = '';
}

function scheduleRecordingPoll(sn) {
    stopRecordingPolling();
    recordingPollSn = sn;
    recordingPollTimer = setTimeout(() => {
        refreshRecordingStatus(sn, true);
    }, 2000);
}

function renderRecordingStatus(recording) {
    const state = recording.state || 'idle';
    const active = state === 'recording' || state === 'stopping';
    recordingActiveState = state;
    const stateLabels = {
        idle: '空闲',
        recording: '录制中',
        stopping: '停止中',
        stopped: '已停止',
        failed: '失败'
    };
    document.getElementById('recording-state-badge').textContent = stateLabels[state] || state;
    document.getElementById('recording-id').textContent = recording.recording_id || '-';
    document.getElementById('recording-started-at').textContent = formatRecordingTime(recording.started_at);
    document.getElementById('recording-segment-count').textContent = String(recording.segment_count || 0);
    document.getElementById('recording-output-dir').textContent = recording.output_dir || recording.relative_output_dir || '-';
    const actionButton = document.getElementById('recording-action-button');
    const durationInput = document.getElementById('recording-segment-seconds');
    const pathInput = document.getElementById('recording-output-root');
    if (state === 'recording') {
        actionButton.textContent = '停止录像';
        actionButton.dataset.mode = 'stop';
        actionButton.disabled = false;
        actionButton.classList.add('recording-stop-button');
    } else if (state === 'stopping') {
        actionButton.textContent = '停止中...';
        actionButton.dataset.mode = 'stop';
        actionButton.disabled = true;
        actionButton.classList.add('recording-stop-button');
    } else {
        actionButton.textContent = '开始录像';
        actionButton.dataset.mode = 'start';
        actionButton.disabled = false;
        actionButton.classList.remove('recording-stop-button');
    }
    durationInput.disabled = active;
    pathInput.disabled = active;
    if (recording.segment_seconds) {
        durationInput.value = recording.segment_seconds;
    }
    if (recording.recording_dir) {
        pathInput.value = recording.recording_dir;
        localStorage.setItem('cameraDecryptorRecordingDir', recording.recording_dir);
    }
    updateRecordingConfigDisplay(recording.config_id !== undefined ? recording.config_id : null);

    const message = document.getElementById('recording-message');
    if (recording.error) {
        message.textContent = recording.error;
        message.style.color = '#b91c1c';
    } else if (active) {
        message.textContent = `正在录制，每 ${formatSegmentDuration(recording.segment_seconds)} 生成一个 MP4 分片。点击“停止录像”即可结束。`;
        message.style.color = '';
    } else if (state === 'stopped') {
        message.textContent = '录像已停止，最后一个 MP4 分片已完成封装。';
        message.style.color = '';
    } else {
        message.textContent = '请选择保存路径和摄像机。录像文件会按日期保存到 `YYYY-MM-DD` 目录。';
        message.style.color = '';
    }

    if (active) {
        scheduleRecordingPoll(recording.sn || getRecordingCameraSn());
    } else {
        stopRecordingPolling();
    }
}

async function refreshRecordingStatus(sn = getRecordingCameraSn(), quiet = false) {
    if (!sn) {
        stopRecordingPolling();
        renderRecordingStatus({ state: 'idle' });
        return;
    }
    if (recordingPollSn && recordingPollSn !== sn) {
        stopRecordingPolling();
    }
    try {
        const response = await fetch(buildApiUrl(`/api/recordings/status?sn=${encodeURIComponent(sn)}`), {
            cache: 'no-store'
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.error) {
            throw new Error(result.error || `HTTP ${response.status}`);
        }
        if (getRecordingCameraSn() !== sn) {
            return;
        }
        renderRecordingStatus(result.recording || { sn, state: 'idle' });
    } catch (error) {
        stopRecordingPolling();
        if (!quiet) {
            log(`查询录像状态失败: ${error.message}`, 'error');
        }
    }
}

async function startRecording() {
    const sn = getRecordingCameraSn();
    if (!sn) {
        log('请先选择或输入摄像机 SN', 'error');
        return;
    }
    const durationInput = document.getElementById('recording-segment-seconds');
    const segmentSeconds = Number(durationInput.value);
    if (!Number.isInteger(segmentSeconds) || segmentSeconds < 10 || segmentSeconds > 86400) {
        log('分片时长必须是 10-86400 之间的整数秒', 'error');
        durationInput.focus();
        return;
    }
    const configId = currentConfigId !== null ? currentConfigId : 0;
    const recordingDir = document.getElementById('recording-output-root').value.trim();
    if (!recordingDir) {
        log('请设置录像文件保存路径', 'error');
        document.getElementById('recording-output-root').focus();
        return;
    }
    localStorage.setItem('cameraDecryptorSelectedSn', sn);
    localStorage.setItem('cameraDecryptorRecordingDir', recordingDir);
    const actionButton = document.getElementById('recording-action-button');
    actionButton.disabled = true;
    actionButton.textContent = '启动中...';
    try {
        const response = await fetch(buildApiUrl('/api/recordings/start'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sn, config_id: configId, segment_seconds: segmentSeconds, recording_dir: recordingDir })
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.error) {
            if (result.recording) {
                renderRecordingStatus(result.recording);
            }
            throw new Error(result.error || `HTTP ${response.status}`);
        }
        renderRecordingStatus(result.recording);
        log(`录像已启动: sn=${sn}, config=${configId}, segment=${segmentSeconds}s`, 'success');
    } catch (error) {
        log(`启动录像失败: ${error.message}`, 'error');
        await refreshRecordingStatus(sn, true);
    }
}

function toggleRecording() {
    if (recordingActiveState === 'recording' || document.getElementById('recording-action-button').dataset.mode === 'stop') {
        stopRecording();
    } else {
        startRecording();
    }
}

async function stopRecording() {
    const sn = getRecordingCameraSn();
    if (!sn) {
        return;
    }
    document.getElementById('recording-action-button').disabled = true;
    document.getElementById('recording-action-button').textContent = '停止中...';
    document.getElementById('recording-state-badge').textContent = '停止中';
    try {
        const response = await fetch(buildApiUrl(`/api/recordings/${encodeURIComponent(sn)}/stop`), {
            method: 'POST'
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.error) {
            throw new Error(result.error || `HTTP ${response.status}`);
        }
        renderRecordingStatus(result.recording || { sn, state: 'idle' });
        log(result.stopped ? '录像已停止' : '当前摄像机没有活动录像', 'success');
    } catch (error) {
        log(`停止录像失败: ${error.message}`, 'error');
        refreshRecordingStatus(sn, true);
    }
}

async function fetchGo2rtcConfig(explicitSn = '') {
    const sn = explicitSn || getCurrentCameraSn();
    const mode = document.getElementById('go2rtc-mode').value || 'decrypted';
    const configId = currentConfigId !== null ? currentConfigId : 0;
    if (!sn) {
        log('请先输入摄像机 SN，再生成 go2rtc 配置', 'error');
        alert('请先输入摄像机 SN');
        return;
    }

    try {
        const response = await fetch(buildApiUrl(`/api/go2rtc/config?sn=${encodeURIComponent(sn)}&mode=${encodeURIComponent(mode)}&config_id=${encodeURIComponent(configId)}`));
        const result = await response.json();
        if (!response.ok || result.error) {
            const errorMessage = result.error || '生成 go2rtc 配置失败';
            const shouldFallback = response.status === 404 || errorMessage.includes('文件不存在: api/go2rtc/config');
            if (shouldFallback) {
                applyGo2rtcConfig(buildLocalGo2rtcConfig(sn), '前端兜底');
                log('检测到后端仍是旧版本，已自动切换为前端生成 go2rtc 配置', 'warning');
                return;
            }
            throw new Error(errorMessage);
        }

        applyGo2rtcConfig(result, '后端接口');
    } catch (error) {
        log(`生成 go2rtc 配置失败: ${error.message}`, 'error');
    }
}

async function copyGo2rtcYaml() {
    const yamlText = document.getElementById('go2rtc-yaml').value.trim();
    if (!yamlText) {
        log('当前没有可复制的 go2rtc 配置', 'warning');
        return;
    }

    try {
        await navigator.clipboard.writeText(yamlText);
        log('go2rtc YAML 已复制到剪贴板', 'success');
    } catch (error) {
        log(`复制失败: ${error.message}`, 'error');
    }
}

// ==================== 清空输入 ====================
function clearInput() {
    document.getElementById('json-input').value = '';
    log('输入已清空', 'info');
}

// ==================== 加载示例数据 ====================
function loadSampleData() {
    const sampleData = {
        "errorCode": 0,
        "playKey": "解密密钥",
        "relay": ["中继服务器地址1", "中继服务器地址2", "中继服务器地址3"],
        "relayId": "中继ID",
        "relaySig": "中继签名",
        "relayStream": "中继流标识",
        "flashUrl": "视频流完整URL",
        "errorMsg": "成功",
        "data": {}
    };

    document.getElementById('json-input').value = JSON.stringify(sampleData, null, 4);
    log('示例数据已加载', 'success');
}

// ==================== 获取配置 ====================
function getConfigs() {
    return [
        {
            id: 0,
            name: '默认解密方式',
            keyType: 0,
            key: apiResponse.playKey,
            keyForKey: null,
            description: 'keyType=0, 使用playKey'
        },
        {
            id: 1,
            name: '解密方式1',
            keyType: 1,
            key: apiResponse.playKey,
            keyForKey: null,
            description: 'keyType=1, 使用playKey'
        },
        {
            id: 2,
            name: '不使用密钥',
            keyType: 0,
            key: null,
            keyForKey: null,
            description: 'keyType=0, key=null (测试未加密流)'
        },
        {
            id: 3,
            name: '使用中继签名',
            keyType: 0,
            key: apiResponse.playKey,
            keyForKey: apiResponse.relaySig,
            description: 'keyType=0, 使用relaySig作为keyForKey'
        }
    ];
}

// ==================== 更新状态 ====================
function updateStatus(status) {
    document.getElementById('player-status').textContent = status;
}

function updateBackendPlayerStatus(status) {
    const statusEl = document.getElementById('backend-player-status');
    if (statusEl) {
        statusEl.textContent = status;
    }
}

// ==================== 更新当前配置显示 ====================
function updateCurrentConfig(config) {
    document.getElementById('current-config').textContent = config.name;

    document.querySelectorAll('.config-item').forEach(item => {
        item.classList.remove('active-config');
    });
    document.getElementById(`config-${config.id}`).classList.add('active-config');
    updateRecordingConfigDisplay(config.id);
}

// ==================== 停止当前播放器 ====================
function stopCurrentPlayer() {
    if (currentPlayer) {
        try {
            currentPlayer.stop();
        } catch (e) {
            console.error('停止播放器失败:', e);
        }
        currentPlayer = null;
    }
}

function buildBackendDecryptedStreamUrl(configId, sn, refresh = false) {
    const query = new URLSearchParams({
        fps: '12',
        format: 'mp4',
        t: String(Date.now())
    });
    if (refresh) {
        query.set('refresh', '1');
        query.set('replace', '1');
    }
    return buildApiUrl(`/api/decrypted-stream/${encodeURIComponent(configId)}/${encodeURIComponent(sn)}?${query.toString()}`);
}

async function stopBackendDecryptedStream() {
    const video = document.getElementById('backend-decrypted-video');
    const sn = backendStreamSn || getCurrentCameraSn();
    const configId = backendStreamConfigId !== null ? backendStreamConfigId : (currentConfigId !== null ? currentConfigId : 0);

    if (backendStreamAbortController) {
        backendStreamAbortController.abort();
        backendStreamAbortController = null;
    }
    if (video) {
        video.pause();
        video.removeAttribute('src');
        video.load();
    }
    if (backendStreamObjectUrl) {
        URL.revokeObjectURL(backendStreamObjectUrl);
        backendStreamObjectUrl = '';
    }

    if (!sn) {
        updateBackendPlayerStatus('已停止');
        return;
    }

    try {
        const response = await fetch(buildApiUrl(`/api/decrypted-stream/${encodeURIComponent(configId)}/${encodeURIComponent(sn)}/stop`), {
            method: 'POST'
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok || result.error) {
            throw new Error(result.error || '停止后端解密流失败');
        }
        if (result.source_kept_alive) {
            log('播放器已停止，公共解密流由录像继续占用', 'success');
        } else {
            log(result.closed ? '后端解密流进程已停止' : '后端没有正在运行的解密流', 'success');
        }
    } catch (error) {
        log(`停止后端解密流失败: ${error.message}`, 'error');
    } finally {
        backendStreamConfigId = null;
        backendStreamSn = '';
        updateBackendPlayerStatus('已停止');
    }
}

async function testBackendDecryptedStream(refresh = false) {
    if (!apiResponse) {
        log('请先获取或解析播放信息', 'error');
        alert('请先获取或解析播放信息');
        return;
    }

    const sn = getCurrentCameraSn();
    if (!sn) {
        log('缺少摄像机 SN，无法启动后端解密流', 'error');
        return;
    }

    const configId = currentConfigId !== null ? currentConfigId : 0;
    const video = document.getElementById('backend-decrypted-video');
    if (!video) {
        log('未找到后端解密流播放器节点', 'error');
        return;
    }

    await stopBackendDecryptedStream();

    backendStreamConfigId = configId;
    backendStreamSn = sn;
    const streamUrl = buildBackendDecryptedStreamUrl(configId, sn, refresh);
    updateBackendPlayerStatus(refresh ? '刷新启动中...' : '启动中...');
    log(`启动后端解密流: config=${configId}, sn=${sn}, refresh=${refresh ? '1' : '0'}`, 'info');

    video.onloadedmetadata = () => {
        updateBackendPlayerStatus('已加载');
        log('后端解密流已加载元数据', 'success');
    };
    video.onplaying = () => {
        updateBackendPlayerStatus('播放中');
        log('后端解密流开始播放', 'success');
    };
    video.onerror = () => {
        updateBackendPlayerStatus('播放失败');
        log('后端解密流播放失败，请查看后端日志中的 Node/ffmpeg 输出', 'error');
    };

    startBackendFmp4Playback(video, streamUrl);
}

function startBackendFmp4Playback(video, streamUrl) {
    // 让浏览器原生 MP4 demuxer 处理 HTTP 分块与 moof/mdat 边界。手工把 fetch
    // 返回的任意网络块逐个 append 到 SourceBuffer，会在真实流首个分片处触发
    // SourceBuffer error，并可能让后端 remux 在断开后继续残留。
    backendStreamAbortController = null;
    backendStreamObjectUrl = '';
    video.crossOrigin = 'anonymous';
    video.src = streamUrl;
    video.load();
    updateBackendPlayerStatus('接收数据中...');
    video.play().catch((error) => {
        updateBackendPlayerStatus('等待手动播放');
        log(`后端解密流等待手动播放: ${error.message}`, 'warning');
    });
}

// ==================== 测试配置 ====================
function testConfig(configId) {
    if (!apiResponse) {
        log('请先解析JSON数据', 'error');
        alert('请先解析JSON数据');
        return;
    }

    if (!checkPlayerLoaded()) {
        updateStatus('播放器未加载');
        return;
    }

    const configs = getConfigs();
    const config = configs[configId];

    log(`开始测试配置 ${configId}: ${config.name}`, 'info');
    updateCurrentConfig(config);

    stopCurrentPlayer();
    updateStatus('正在初始化...');

    const container = document.getElementById('video-container');

    try {
        const streamUrl = apiResponse.flashUrl || apiResponse.sourceFlashUrl;

        const playerConfig = {
            container: container,
            src: streamUrl,
            key: config.key,
            keyType: config.keyType,
            isLive: true,
            autoplay: true,
            logLevel: 2,
            renderType: 'all',
            resample: 0
        };

        if (config.keyForKey) {
            playerConfig.keyForKey = config.keyForKey;
        }

        log(`播放器配置: ${JSON.stringify(playerConfig)}`, 'info');

        currentPlayer = new QhwwPlayer(playerConfig);

        currentPlayer.on({
            ready: () => {
                log('播放器就绪', 'success');
                updateStatus('播放器就绪');
            },
            play: () => {
                log('开始播放', 'success');
                updateStatus('正在播放');
            },
            pause: () => {
                log('暂停播放', 'info');
                updateStatus('已暂停');
            },
            stop: () => {
                log('停止播放', 'info');
                updateStatus('已停止');
            },
            error: (error) => {
                log(`播放错误: ${error}`, 'error');
                updateStatus('播放错误');
            },
            timeupdate: () => {
            }
        });

        currentConfigId = configId;
    } catch (e) {
        log(`创建播放器失败: ${e.message}`, 'error');
        updateStatus('创建播放器失败');
    }
}

// ==================== 停止所有测试 ====================
function stopAllTests() {
    log('停止所有测试', 'info');
    stopCurrentPlayer();
    stopBackendDecryptedStream();
    currentConfigId = null;
    updateRecordingConfigDisplay(0);
    updateStatus('已停止');
    document.querySelectorAll('.config-item').forEach(item => {
        item.classList.remove('active-config');
    });
}

// ==================== 清空日志 ====================
function clearLogs() {
    document.getElementById('log-container').innerHTML = '';
    log('日志已清空', 'info');
}

// ==================== 初始化 ====================
document.addEventListener('DOMContentLoaded', function() {
    document.getElementById('backend-base-url').value = getDefaultBackendBaseUrl();
    log('=== 360智能摄像机视频流解密工具 ===', 'success');
    log('系统初始化完成', 'success');
    log('页面已切换为后端驱动模式，建议先加载摄像机列表，再获取播放信息', 'info');

    setTimeout(function() {
        checkPlayerLoaded();
    }, 1000);

    loadCameraList();
    loadRecordingSettings();

    document.getElementById('camera-sn-input').addEventListener('change', function() {
        const sn = this.value.trim();
        if (sn) {
            localStorage.setItem('cameraDecryptorSelectedSn', sn);
        }
        refreshRecordingStatus(sn);
    });

    log('');
    updateStatus('等待输入');
});
