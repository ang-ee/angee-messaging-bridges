import { expectChannelVerbsScoped, expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { describe, expect, test } from "vitest";

import messagingIntegrateWhatsapp from "./index";

describe("messaging_integrate_whatsapp addon manifest", () => {
  test("declares a valid bridge at the WhatsApp implementation key", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateWhatsapp)).not.toThrow();
    expect(() => expectChannelVerbsScoped(messagingIntegrateWhatsapp, "whatsapp")).not.toThrow();
  });

  test("contributes WhatsApp scan copy", () => {
    expect(messagingIntegrateWhatsapp.i18n?.messaging?.["channel.whatsapp.scan"]).toContain(
      "Linked devices",
    );
  });
});
