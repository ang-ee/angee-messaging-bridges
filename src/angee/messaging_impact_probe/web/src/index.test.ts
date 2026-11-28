import { expectValidBaseAddon } from "@angee/app/testing";
import { describe, expect, test } from "vitest";

import addon from ".";

describe("messaging_impact_probe addon manifest", () => {
  test("satisfies the shared manifest invariants", () => {
    expect(() => expectValidBaseAddon(addon)).not.toThrow();
  });
});
