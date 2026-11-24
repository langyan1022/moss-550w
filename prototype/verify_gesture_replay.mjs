/**
 * Continuous 21-Landmark Hand-Gesture Replay Regression (Stage 2 Unit Gate)
 * Models anatomical flexor-tendon coupling, hand translation sweep, wrist tilt,
 * and jitter to verify strict separation between 3-finger rail pan and 5-finger palm pose.
 */
import assert from 'node:assert/strict';
import { classifyGesture } from './fui/public/js/gestures.js';

function makeAnatomicalHand({
  palmX = 0.5,
  palmY = 0.6,
  scale = 0.25,
  tilt = 0,
  thumbOut = true,
  indexExt = false,
  middleExt = false,
  ringExt = false,
  pinkyRatio = 0.65,
  pinch = false,
  jitter = 0,
}) {
  const pts = Array.from({ length: 21 }, () => ({ x: palmX, y: palmY }));
  const rot = (dx, dy) => ({
    x: palmX + dx * Math.cos(tilt) - dy * Math.sin(tilt) + jitter,
    y: palmY + dx * Math.sin(tilt) + dy * Math.cos(tilt) - jitter * 0.5,
  });
  pts[0] = rot(0, scale);
  pts[9] = { x: palmX, y: palmY };

  pts[2] = rot(-0.6 * scale, 0.2 * scale);
  pts[4] = pinch
    ? rot(-0.22 * scale, -1.04 * scale)
    : thumbOut
      ? rot(-1.12 * scale, 0.08 * scale)
      : rot(-0.25 * scale, 0.25 * scale);

  const setFinger = (pipIdx, tipIdx, xOff, reachRatio) => {
    const pip = rot(xOff * scale, -0.2 * scale);
    const dx = pip.x - pts[0].x;
    const dy = pip.y - pts[0].y;
    pts[pipIdx] = pip;
    pts[tipIdx] = {
      x: pts[0].x + dx * reachRatio,
      y: pts[0].y + dy * reachRatio,
    };
  };

  setFinger(6, 8, -0.28, indexExt ? 1.72 : 0.78);
  if (pinch) {
    pts[8] = rot(-0.18 * scale, -1.0 * scale);
  }
  setFinger(10, 12, 0.0, middleExt ? 1.75 : 0.76);
  setFinger(14, 16, 0.28, ringExt ? 1.68 : 0.75);
  setFinger(18, 20, 0.56, pinkyRatio);
  return pts;
}

const results = { railTendon: 0, palmOpen: 0, relaxedPeace: 0, pinch: 0, point: 0, peace: 0, fist: 0 };

// 1. 30 continuous frames of tendon-coupled 3-finger 'rail' (pinky partially follows ring: ratio 1.12..1.30 < 0.85 * 1.68 = 1.428)
for (let i = 0; i < 30; i++) {
  const t = i / 29;
  const x = 0.76 - 0.52 * t;
  const y = 0.58 + Math.sin(t * Math.PI * 2) * 0.03;
  const tilt = (t - 0.5) * 0.28;
  const coupledPinky = 1.12 + 0.18 * ((i % 5) / 4); // > 1.10 old threshold, still clearly flexed vs 1.68 ring!
  const hand = makeAnatomicalHand({
    palmX: x, palmY: y, tilt,
    indexExt: true, middleExt: true, ringExt: true,
    pinkyRatio: coupledPinky,
    jitter: ((i % 3) - 1) * 0.002,
  });
  const g = classifyGesture(hand);
  assert.equal(g, 'rail', `Frame ${i} tendon-coupled 3-finger leaked as ${g} (pinky=${coupledPinky})`);
  results.railTendon++;
}

// 2. 20 continuous frames of open 5-finger 'palm' (pinky extended >= 0.85 * ringReach)
for (let i = 0; i < 20; i++) {
  const t = i / 19;
  const hand = makeAnatomicalHand({
    palmX: 0.3 + 0.4 * t,
    palmY: 0.45 + 0.15 * Math.sin(t * Math.PI),
    tilt: -0.18 + 0.36 * t,
    indexExt: true, middleExt: true, ringExt: true,
    pinkyRatio: 1.54 + 0.16 * ((i % 4) / 3),
  });
  const g = classifyGesture(hand);
  assert.equal(g, 'palm', `Frame ${i} open palm misclassified as ${g}`);
  results.palmOpen++;
}

// 3. 12 frames of relaxed-thumb V-sign (Index + Middle extended, Thumb relaxed out) -> consistently 'peace', zero false 'rail'
for (let i = 0; i < 12; i++) {
  const hand = makeAnatomicalHand({
    palmX: 0.35 + 0.025 * i,
    thumbOut: true, indexExt: true, middleExt: true, ringExt: false, pinkyRatio: 0.76,
  });
  const g = classifyGesture(hand);
  assert.equal(g, 'peace', `Frame ${i} relaxed-thumb V-sign misclassified as ${g}`);
  results.relaxedPeace++;
}

// 4. 15 frames of pinch zoom sweep
for (let i = 0; i < 15; i++) {
  const hand = makeAnatomicalHand({
    palmY: 0.40 + 0.015 * i,
    thumbOut: false, indexExt: true, middleExt: true, ringExt: true, pinkyRatio: 1.58, pinch: true,
  });
  const g = classifyGesture(hand);
  assert.equal(g, 'pinch', `Frame ${i} pinch misclassified as ${g}`);
  results.pinch++;
}

// 5. Point, Peace, Fist, & null/malformed boundary frames
for (let i = 0; i < 10; i++) {
  assert.equal(classifyGesture(makeAnatomicalHand({ indexExt: true, pinkyRatio: 0.75 })), 'point');
  results.point++;
  assert.equal(classifyGesture(makeAnatomicalHand({ indexExt: true, middleExt: true, thumbOut: false, pinkyRatio: 0.75 })), 'peace');
  results.peace++;
  assert.equal(classifyGesture(makeAnatomicalHand({ thumbOut: false, pinkyRatio: 0.75 })), 'fist');
  results.fist++;
}
assert.equal(classifyGesture(null), 'none');
assert.equal(classifyGesture([]), 'none');

console.log(`GESTURE_REPLAY_OK: ${JSON.stringify(results)}`);
