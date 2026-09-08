/**
 * 360智能摄像机视频流解密修复方案
 * 
 * 问题：画面颜色整体成绿色调
 * 原因分析：
 * 1. keyType 可能需要不同的值
 * 2. 可能需要额外的解密参数
 * 3. 视频流可能需要特定的格式配置
 */

class CameraDecryptorFixed {
    constructor() {
        // API基础URL - 请根据实际情况配置
        this.baseUrl = 'https://my.jia.360.cn';
        this.videoBaseUrl = 'https://flv-live.jia.360.cn/live_jia_personal/';
    }

    /**
     * 提取播放信息（修复版）
     * @param {Object} apiResponse - API返回的数据
     * @returns {Object} 播放信息
     */
    extractPlayInfo(apiResponse) {
        if (apiResponse.errorCode !== 0) {
            return {
                success: false,
                errorCode: apiResponse.errorCode,
                errorMsg: apiResponse.errorMsg || '获取播放信息失败'
            };
        }

        const relayStream = apiResponse.relayStream || '';
        const playKey = apiResponse.playKey || '';
        const flashUrl = apiResponse.flashUrl || '';
        const relay = apiResponse.relay || [];
        const relayId = apiResponse.relayId || '';
        const relaySig = apiResponse.relaySig || '';
        
        // 优先使用flashUrl，如果不存在则使用relayStream拼接
        let videoUrl = flashUrl;
        if (!videoUrl && relayStream) {
            videoUrl = `${this.videoBaseUrl}${relayStream}.flv`;
        }

        // 尝试不同的解密配置
        const configs = [
            // 配置1: keyType = 0（默认）
            {
                keyType: 0,
                key: playKey,
                description: '默认解密方式 (keyType=0)'
            },
            // 配置2: keyType = 1
            {
                keyType: 1,
                key: playKey,
                description: '解密方式1 (keyType=1)'
            },
            // 配置3: 不使用密钥
            {
                keyType: 0,
                key: null,
                description: '不使用密钥 (测试用)'
            },
            // 配置4: 使用keyForKey
            {
                keyType: 0,
                key: playKey,
                keyForKey: relaySig,  // 使用中继签名作为keyForKey
                description: '使用中继签名作为keyForKey'
            }
        ];

        return {
            success: true,
            videoUrl: videoUrl,
            flashUrl: flashUrl,
            relayStream: relayStream,
            playKey: playKey,
            isEncrypted: !!playKey,
            keyLength: playKey ? playKey.length : 0,
            relay: relay,
            relayId: relayId,
            relaySig: relaySig,
            configs: configs,  // 多种配置供测试
            rawData: apiResponse
        };
    }

    /**
     * 创建播放器（修复版）
     * @param {string} videoUrl - 视频流地址
     * @param {Object} config - 播放器配置
     * @param {HTMLElement} container - 容器元素
     * @returns {Object} 播放器实例
     */
    createPlayer(videoUrl, config, container) {
        if (typeof QhwwPlayer === 'undefined') {
            console.error('QhwwPlayer 未加载，请先加载播放器库');
            return null;
        }

        const playerConfig = {
            container: container,
            src: videoUrl,
            key: config.key || null,
            keyType: config.keyType || 0,
            isLive: true,
            autoplay: true,
            logLevel: 0,
            // 尝试其他可能影响颜色的参数
            renderType: 'all',  // 渲染类型
            resample: 1,
            sei: 0,             // SEI
            minDecoderBufferSize: 1,
            waitingPcmDur: 50,
            waitingYuvNum: 1,
            delayTimeLimit: 1000,
            maxDecoderVCacheLength: 300,
            maxDecoderACacheLength: 500
        };

        // 如果有keyForKey，添加到配置中
        if (config.keyForKey) {
            playerConfig.keyForKey = config.keyForKey;
        }

        console.log('使用配置:', config.description);

        const player = new QhwwPlayer(playerConfig);

        return player;
    }

    /**
     * 测试不同配置
     * @param {Object} playInfo - 播放信息
     * @param {HTMLElement} container - 容器元素
     */
    testConfigs(playInfo, container) {
        console.log('=== 开始测试不同配置 ===\n');

        playInfo.configs.forEach((config, index) => {
            console.log(`\n配置 ${index + 1}: ${config.description}`);
            
            // 清空容器
            container.innerHTML = '';
            
            // 创建播放器
            const player = this.createPlayer(playInfo.videoUrl, config, container);
            
            if (player) {
                // 监听播放器事件
                player.on({
                    ready: () => {
                        console.log('  ✓ 播放器就绪');
                    },
                    play: () => {
                        console.log('  ✓ 开始播放');
                    },
                    error: (error) => {
                        console.error('  ✗ 播放错误:', error);
                    },
                    timeupdate: (data) => {
                        // 可以在这里检查画面颜色
                    }
                });

                // 播放
                player.play();

                // 5秒后停止，测试下一个配置
                setTimeout(() => {
                    console.log(`  → 停止配置 ${index + 1}`);
                    player.stop().catch(err => console.error('  ✗ 停止失败:', err));
                }, 5000);
            }
        });
    }
}

// 导出供外部使用
if (typeof module !== 'undefined' && module.exports) {
    module.exports = CameraDecryptorFixed;
}

// 使用示例：
/*
const decryptor = new CameraDecryptorFixed();

const apiResponse = {
    "errorCode": 0,
    "playKey": "解密密钥",
    "relayStream": "中继流标识",
    "flashUrl": "视频流完整URL",
    "relay": ["中继服务器列表"],
    "relayId": "中继ID",
    "relaySig": "中继签名",
    "errorMsg": "成功",
    "data": {}
};

const playInfo = decryptor.extractPlayInfo(apiResponse);
console.log('播放信息:', playInfo);

const container = document.getElementById('video-container');

// 测试不同配置
decryptor.testConfigs(playInfo, container);
*/
