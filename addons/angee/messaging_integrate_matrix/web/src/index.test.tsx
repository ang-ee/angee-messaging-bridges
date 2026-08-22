import { CHANNEL_MODEL } from "@angee/messaging";
import { expectValidChannelBridgeAddon } from "@angee/messaging/testing";
import { formViewRecordActionsSlot } from "@angee/ui";
import { describe, expect, test } from "vitest";

import messagingIntegrateMatrix from "./index";

describe("messaging_integrate_matrix addon manifest", () => {
  test("declares a valid bridge at the Matrix implementation key", () => {
    expect(() => expectValidChannelBridgeAddon(messagingIntegrateMatrix)).not.toThrow();
    const actions = (messagingIntegrateMatrix.slots ?? []).slice(1);
    expect(actions.map(({ slot, model, impl }) => ({ slot, model, impl }))).toEqual(
      actions.map(() => formViewRecordActionsSlot(CHANNEL_MODEL, "matrix")),
    );
  });

  test("contributes Matrix navigation and recovery-key copy", () => {
    expect(messagingIntegrateMatrix.menus?.[0]?.description).toBe("Connect your Matrix account");
    expect(messagingIntegrateMatrix.i18n?.messaging?.["channel.matrix.recovery"]).toContain(
      "recovery key",
    );
  });
});
