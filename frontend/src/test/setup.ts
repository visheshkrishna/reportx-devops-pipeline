import '@testing-library/jest-dom/vitest';

const createCanvasContextMock = (canvas: HTMLCanvasElement) => ({
	canvas,
	clearRect: () => undefined,
	fillRect: () => undefined,
	strokeRect: () => undefined,
	beginPath: () => undefined,
	closePath: () => undefined,
	moveTo: () => undefined,
	lineTo: () => undefined,
	bezierCurveTo: () => undefined,
	quadraticCurveTo: () => undefined,
	arc: () => undefined,
	rect: () => undefined,
	fill: () => undefined,
	stroke: () => undefined,
	save: () => undefined,
	restore: () => undefined,
	translate: () => undefined,
	scale: () => undefined,
	rotate: () => undefined,
	setTransform: () => undefined,
	resetTransform: () => undefined,
	drawImage: () => undefined,
	measureText: (text: unknown) => ({ width: String(text ?? '').length * 8 }),
	setLineDash: () => undefined,
	getLineDash: () => [],
	fillText: () => undefined,
	strokeText: () => undefined,
	createLinearGradient: () => ({ addColorStop: () => undefined }),
	createRadialGradient: () => ({ addColorStop: () => undefined }),
	createPattern: () => null,
});

Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
	value(this: HTMLCanvasElement) {
		return createCanvasContextMock(this);
	},
});

class ResizeObserverMock {
	observe() {}
	unobserve() {}
	disconnect() {}
}

(globalThis as typeof globalThis & { ResizeObserver: typeof ResizeObserverMock }).ResizeObserver = ResizeObserverMock;


// CI containers run slower than local dev machines. Raise Testing Library's
// default 1000ms async timeout so waitFor() assertions don't fail on timing alone.
import { configure } from "@testing-library/dom";
configure({ asyncUtilTimeout: 5000 });
