import { expectValidBaseAddon } from "@angee/app/testing";
import { CHANNEL_MODEL, MESSAGING_CHANNEL_TOOLBAR_SLOT } from "@angee/messaging";
import { formViewRecordActionsSlot } from "@angee/ui";
import { describe, expect, test } from "vitest";

import messagingIntegrateSignal from "./index";

describe("messaging_integrate_signal addon manifest", () => {
  test("declares a valid bridge at the Signal implementation key", () => {
    expect(() => expectValidBaseAddon(messagingIntegrateSignal)).not.toThrow();
    expect(messagingIntegrateSignal.slots?.[0]).toMatchObject({
      slot: MESSAGING_CHANNEL_TOOLBAR_SLOT,
      id: "messaging-integrate-signal.connect",
      sequence: 22,
    });
    const actions = (messagingIntegrateSignal.slots ?? []).slice(1);
    expect(actions.map(({ slot, model, impl }) => ({ slot, model, impl }))).toEqual(
      actions.map(() => formViewRecordActionsSlot(CHANNEL_MODEL, "signal")),
    );
  });

  test("contributes Signal navigation and scan copy", () => {
    expect(messagingIntegrateSignal.menus?.[0]).toMatchObject({
      id: "messaging.signal",
      label: "Signal",
      parentId: "messaging",
      description: "Link Signal accounts by QR code",
    });
    expect(messagingIntegrateSignal.i18n?.messaging?.["channel.signal.scan"]).toContain(
      "Linked Devices",
    );
  });
});
