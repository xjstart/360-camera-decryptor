/**
 * 360智能摄像机视频流解密工具
 * 
 * 分析结果：
 * 1. 视频流使用 playKey 进行加密
 * 2. 解密在 WASM 模块中进行
 * 3. keyType 指定解密算法类型
 */

class CameraDecryptor {
    constructor() {
        // API基础URL - 请根据实际情况配置
        this.baseUrl = 'https://my.jia.360.cn';
        this.videoBaseUrl = 'https://flv-live.jia.360.cn/live_jia_personal/';
    }

    /**
     * 获取播放信息（包括密钥和流地址）
     * @param {string} sn - 摄像机SN号
     * @param {boolean} isV2 - 是否使用V2接口
     * @returns {Promise<Object>} 播放信息
     */
    async getPlayInfo(sn, isV2 = false) {
        // API接口路径 - 请根据实际情况配置
        const url = isV2 ? '/app/playV2' : '/app/play';
        const params = new URLSearchParams({
            taskid: Date.now(),
            from: 'mpc_ipcam_web',
            sn: sn,
            mode: 0
        });

        try {
            const response = await fetch(`${this.baseUrl}${url}?${params.toString()}`, {
                method: 'GET',
                credentials: 'include',
                headers: {
                    'Accept': 'application/json',
                }
            });

            const data = await response.json();
            
            if (data.errorCode === 0) {
                // 处理实际返回的数据格式
                const relayStream = data.relayStream || '';
                const playKey = data.playKey || '';
                const flashUrl = data.flashUrl || '';
                const relay = data.relay || [];
                const relayId = data.relayId || '';
                const relaySig = data.relaySig || '';
                
                // 优先使用flashUrl，如果不存在则使用relayStream拼接
                let videoUrl = flashUrl;
                if (!videoUrl && relayStream) {
                    videoUrl = `${this.videoBaseUrl}${relayStream}.flv`;
                }

                return {
                    success: true,
                    relayStream: relayStream,
                    playKey: playKey,
                    videoUrl: videoUrl,
                    flashUrl: flashUrl,
                    relay: relay,
                    relayId: relayId,
                    relaySig: relaySig,
                    keyType: 0, // 默认解密方式
                    isEncrypted: !!playKey,
                    rawData: data // 返回原始数据供调试使用
                };
            } else {
                return {
                    success: false,
                    errorCode: data.errorCode,
                    errorMsg: data.errorMsg || '获取播放信息失败'
                };
            }
        } catch (error) {
            return {
                success: false,
                error: error.message,
                errorMsg: '网络请求失败'
            };
        }
    }

    /**
     * 创建播放器实例
     * @param {string} videoUrl - 视频流地址
     * @param {string} playKey - 播放密钥
     * @param {HTMLElement} container - 容器元素
     * @returns {Object} 播放器实例
     */
    createPlayer(videoUrl, playKey, container) {
        // 检查 QhwwPlayer 是否可用
        if (typeof QhwwPlayer === 'undefined') {
            console.error('QhwwPlayer 未加载，请先加载播放器库');
            return null;
        }

        const player = new QhwwPlayer({
            container: container,
            src: videoUrl,
            key: playKey || null,
            keyType: 0, // 解密方式
            isLive: true,
            autoplay: true,
            logLevel: 0,
            minDecoderBufferSize: 1,
            waitingPcmDur: 50,
            waitingYuvNum: 1,
            delayTimeLimit: 1000,
            resample: 1
        });

        return player;
    }

    /**
     * 解密并播放视频流
     * @param {string} sn - 摄像机SN号
     * @param {string} containerId - 容器元素ID
     */
    async decryptAndPlay(sn, containerId) {
        const container = document.getElementById(containerId);
        if (!container) {
            console.error('找不到容器元素:', containerId);
            return;
        }

        console.log('正在获取播放信息...');
        const playInfo = await this.getPlayInfo(sn);

        if (!playInfo.success) {
            console.error('获取播放信息失败:', playInfo.errorMsg);
            container.innerHTML = `<p style="color: red;">错误: ${playInfo.errorMsg}</p>`;
            return;
        }


        if (playInfo.isEncrypted) {
            console.log('视频流已加密，使用密钥解密');
        } else {
            console.log('视频流未加密');
        }


        // 创建播放器
        const player = this.createPlayer(playInfo.videoUrl, playInfo.playKey, container);
        
        if (player) {
            player.on({
                ready: () => {
                    console.log('播放器就绪');
                },
                play: () => {
                    console.log('开始播放');
                },
                error: (error) => {
                    console.error('播放错误:', error);
                }
            });

            player.play();
        }

        return player;
    }
}

// 导出到全局
window.CameraDecryptor = CameraDecryptor;

// 使用示例：
/*
const decryptor = new CameraDecryptor();

// 方式1: 直接解密并播放
decryptor.decryptAndPlay('摄像机SN号', 'video-container');

// 方式2: 分步骤操作
(async () => {
    // 获取播放信息
    const playInfo = await decryptor.getPlayInfo('摄像机SN号');
    console.log('播放信息:', playInfo);
    
    // 创建播放器
    const container = document.getElementById('video-container');
    const player = decryptor.createPlayer(playInfo.videoUrl, playInfo.playKey, container);
    
    // 播放
    player.play();
})();
*/
