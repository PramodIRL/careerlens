import { describe, expect, it } from "vitest";

import {
  STATUS_LABEL,
  STATUS_STYLES,
  type HealthStatus,
} from "./health-status";

const ALL_STATUSES: HealthStatus[] = ["checking", "ok", "error"];

describe("health-status", () => {
  it("defines a label for every possible status", () => {
    for (const status of ALL_STATUSES) {
      expect(STATUS_LABEL[status]).toBeTruthy();
    }
  });

  it("defines a style for every possible status", () => {
    for (const status of ALL_STATUSES) {
      expect(STATUS_STYLES[status]).toBeTruthy();
    }
  });

  it("gives the ok status a distinct, positive-sounding label", () => {
    expect(STATUS_LABEL.ok).toBe("API is healthy");
    expect(STATUS_STYLES.ok).toContain("green");
  });

  it("gives the error status a distinct, negative-sounding label", () => {
    expect(STATUS_LABEL.error).toBe("API is unreachable");
    expect(STATUS_STYLES.error).toContain("red");
  });
});
