import { expectValidBaseAddon } from "@angee/app/testing";
import { CHANNEL_MODEL, MESSAGING_CHANNEL_TOOLBAR_SLOT } from "@angee/messaging";
import { formViewRecordActionsSlot } from "@angee/ui";
import { describe, expect, test } from "vitest";

import messagingIntegrateWhatsapp from "./index";

describe("messaging_integrate_whatsapp addon manifest", () => {
  test("declares a valid bridge at the WhatsApp implementation key", () => {
    expect(() => expectValidBaseAddon(messagingIntegrateWhatsapp)).not.toThrow();
    expect(messagingIntegrateWhatsapp.slots?.[0]).toMatchObject({
      slot: MESSAGING_CHANNEL_TOOLBAR_SLOT,
      id: "messaging-integrate-whatsapp.connect",
      sequence: 20,
    });
    const actions = (messagingIntegrateWhatsapp.slots ?? []).slice(1);
    expect(actions.map(({ slot, model, impl }) => ({ slot, model, impl }))).toEqual(
      actions.map(() => formViewRecordActionsSlot(CHANNEL_MODEL, "whatsapp")),
    );
  });

  test("contributes WhatsApp navigation and scan copy", () => {
    expect(messagingIntegrateWhatsapp.menus?.[0]).toMatchObject({
      id: "messaging.whatsapp",
      label: "WhatsApp",
      parentId: "messaging",
      description: "Link WhatsApp accounts by QR code",
    });
    expect(messagingIntegrateWhatsapp.i18n?.messaging?.["channel.whatsapp.scan"]).toContain(
      "Linked devices",
    );
  });
});
