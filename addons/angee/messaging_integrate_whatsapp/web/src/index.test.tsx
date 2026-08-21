import { CHANNEL_MODEL } from "@angee/messaging";
import { expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { formViewRecordActionsSlot } from "@angee/ui";
import { describe, expect, test } from "vitest";

import messagingIntegrateWhatsapp from "./index";

describe("messaging_integrate_whatsapp addon manifest", () => {
  test("declares a valid bridge at the WhatsApp implementation key", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateWhatsapp)).not.toThrow();
    const actions = (messagingIntegrateWhatsapp.slots ?? []).slice(1);
    expect(actions.map(({ slot, model, impl }) => ({ slot, model, impl }))).toEqual(
      actions.map(() => formViewRecordActionsSlot(CHANNEL_MODEL, "whatsapp")),
    );
  });

  test("contributes WhatsApp navigation and scan copy", () => {
    expect(messagingIntegrateWhatsapp.menus?.[0]?.description).toBe("Link WhatsApp accounts by QR code");
    expect(messagingIntegrateWhatsapp.i18n?.messaging?.["channel.whatsapp.scan"]).toContain(
      "Linked devices",
    );
  });
});
