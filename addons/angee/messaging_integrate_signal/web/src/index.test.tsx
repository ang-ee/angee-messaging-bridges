import { expectChannelVerbsScoped, expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { describe, expect, test } from "vitest";

import messagingIntegrateSignal from "./index";

describe("messaging_integrate_signal addon manifest", () => {
  test("declares a valid bridge at the Signal implementation key", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateSignal)).not.toThrow();
    expect(() => expectChannelVerbsScoped(messagingIntegrateSignal, "signal")).not.toThrow();
  });

  test("contributes Signal scan copy", () => {
    expect(messagingIntegrateSignal.i18n?.messaging?.["channel.signal.scan"]).toContain(
      "Linked Devices",
    );
  });
});
