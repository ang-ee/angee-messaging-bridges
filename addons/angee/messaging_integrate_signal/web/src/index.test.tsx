import { CHANNEL_MODEL } from "@angee/messaging";
import { expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { formViewRecordActionsSlot } from "@angee/ui";
import { describe, expect, test } from "vitest";

import messagingIntegrateSignal from "./index";

describe("messaging_integrate_signal addon manifest", () => {
  test("declares a valid bridge at the Signal implementation key", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateSignal)).not.toThrow();
    const actions = (messagingIntegrateSignal.slots ?? []).slice(1);
    expect(actions.map(({ slot, model, impl }) => ({ slot, model, impl }))).toEqual(
      actions.map(() => formViewRecordActionsSlot(CHANNEL_MODEL, "signal")),
    );
  });

  test("contributes Signal scan copy", () => {
    expect(messagingIntegrateSignal.i18n?.messaging?.["channel.signal.scan"]).toContain(
      "Linked Devices",
    );
  });
});
