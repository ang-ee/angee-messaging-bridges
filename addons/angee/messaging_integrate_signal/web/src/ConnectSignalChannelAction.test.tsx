// @vitest-environment happy-dom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ActionMenu, Dialog } from "@angee/ui";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const actionMocks = vi.hoisted(() => ({
  authoredMutation: vi.fn(async (_variables: unknown) => ({
    connect_signal_channel: { id: "chn_1" },
  })),
  mutationOptions: null as Record<string, unknown> | null,
  pairingDialogProps: null as Record<string, unknown> | null,
  danger: vi.fn(),
}));

vi.mock("@angee/messaging", () => ({
  usePairingConnect: (_document: unknown, resultField: string, instruction: string) => {
    actionMocks.mutationOptions = {
      invalidateModels: ["messaging.Channel"],
    };
    const [channelId, setChannelId] = React.useState<string | null>(null);
    const connect = async (variables: unknown) => {
      const data = await actionMocks.authoredMutation(variables);
      const result = data[resultField as keyof typeof data];
      if (result?.id) setChannelId(String(result.id));
      return data;
    };
    const props = {
      channelId,
      instruction,
      onClose: () => setChannelId(null),
    };
    actionMocks.pairingDialogProps = props;
    return {
      connect,
      connectState: { fetching: false, error: null },
      pairingDialog: channelId ? (
        <Dialog.Root open onOpenChange={(open) => { if (!open) setChannelId(null); }}>
          <Dialog.Portal>
            <Dialog.Backdrop />
            <Dialog.Content>
              <Dialog.Title>Signal pairing</Dialog.Title>
              <p>{instruction}</p>
              <Dialog.Close>Done</Dialog.Close>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      ) : null,
    };
  },
}));

vi.mock("@angee/ui", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@angee/ui")>()),
  useToast: () => ({ danger: actionMocks.danger }),
  errorMessage: (_error: unknown, fallback: string) => fallback,
}));

vi.mock("./i18n", () => ({
  useMessagingSignalT: () => (key: string) => key,
}));

import { ConnectSignalChannelAction } from "./ConnectSignalChannelAction";

describe("ConnectSignalChannelAction", () => {
  afterEach(cleanup);

  beforeEach(() => {
    actionMocks.authoredMutation.mockClear();
    actionMocks.mutationOptions = null;
    actionMocks.pairingDialogProps = null;
    actionMocks.danger.mockClear();
  });

  test("creates a credential-free channel, then opens shared pairing", async () => {
    render(<ConnectSignalChannelAction />);

    expect(actionMocks.mutationOptions).toEqual({
      invalidateModels: ["messaging.Channel"],
    });
    fireEvent.click(screen.getByRole("button", { name: /channel.signal.button/ }));

    await waitFor(() => expect(actionMocks.authoredMutation).toHaveBeenCalledWith({}));
    await waitFor(() => expect(screen.getByRole("dialog")).toBeTruthy());
    expect(actionMocks.pairingDialogProps).toMatchObject({
      channelId: "chn_1",
      instruction: "channel.signal.scan",
    });
  });

  test("opens pairing from a real Connect menu item and restores focus after dismissal", async () => {
    render(<ActionMenu label="Connect"><ConnectSignalChannelAction /></ActionMenu>);
    const trigger = screen.getByRole("button", { name: "Connect" });
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("menuitem", { name: "channel.signal.button" }));
    expect(await screen.findByRole("dialog", { name: "Signal pairing" })).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(screen.getByRole("dialog", { name: "Signal pairing" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(actionMocks.authoredMutation).toHaveBeenCalledWith({});
  });
});
