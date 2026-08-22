import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Explicit, rather than enabling vitest's `test.globals`: React Testing
// Library's automatic cleanup only self-registers when it detects a
// global `afterEach` (e.g. under Jest's default globals), which vitest
// doesn't provide unless globals are turned on. Without this, unmounted
// component output from a previous test in the same file lingers in the
// document and can make later queries in that file match more than one
// element.
afterEach(() => {
  cleanup();
});
