import { motionValue } from 'motion/react';

/**
 * 跟着鼠标的小纸条的位置。
 *
 * 这里刻意不去写 `document.documentElement` 上的 `--tip-x/--tip-y`：回忆房间里有几百张
 * 毛玻璃纸页（每张都有 backdrop-filter），改根节点的自定义属性会让整页重新计算样式
 * （实测每次约 4ms），鼠标一动就把帧时间挤爆，3D 画布会被丢掉整块、黑屏频闪。
 * 写成 motion 值只改提示框自己的 transform，代价可以忽略。
 */
export const tipX = motionValue(0);
export const tipY = motionValue(0);
