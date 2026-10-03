import { expectChannelVerbsScoped, expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { describe, expect, test } from "vitest";

import messagingIntegrateMatrix from "./index";

describe("messaging_integrate_matrix addon manifest", () => {
  test("declares a valid bridge at the Matrix implementation key", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateMatrix)).not.toThrow();
    expect(() => expectChannelVerbsScoped(messagingIntegrateMatrix, "matrix")).not.toThrow();
  });

  test("contributes Matrix recovery-key copy", () => {
    expect(messagingIntegrateMatrix.i18n?.messaging?.["channel.matrix.recovery"]).toContain(
      "recovery key",
    );
  });
});
