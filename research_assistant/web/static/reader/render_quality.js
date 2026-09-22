// Supersample small text without changing PDF/user-space annotation coordinates.
export function renderPixels(width, height, deviceRatio = 1) {
  const desired = Math.max(2, Math.min(3, deviceRatio || 1));
  const ratio = Math.min(desired, Math.sqrt(16000000 / (width * height)), 8192 / width, 8192 / height);
  const pixelWidth = Math.max(1, Math.floor(width * ratio));
  const pixelHeight = Math.max(1, Math.floor(height * ratio));
  return { width: pixelWidth, height: pixelHeight,
    transform: [pixelWidth / width, 0, 0, pixelHeight / height, 0, 0] };
}
