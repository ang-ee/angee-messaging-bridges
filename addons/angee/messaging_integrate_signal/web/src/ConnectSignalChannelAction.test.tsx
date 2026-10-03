// @vitest-environment happy-dom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ActionMenu } from "@angee/ui";
import * as React from "react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const actionMocks = vi.hoisted(() => ({
  authoredMutation: vi.fn(async (_variables: unknown) => ({
    connect_signal_channel: { id: "chn_1" },
  })),
  mutationOptions: null as Record<string, unknown> | null,
  queryVariables: null as Record<string, unknown> | null,
  danger: vi.fn(),
}));

// Exercise the real usePairingConnect → PairingDialog path; isolate only transport.
vi.mock("@angee/refine", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@angee/refine")>()),
  useAuthoredMutation: (_document: unknown, options: Record<string, unknown>) => {
    actionMocks.mutationOptions = options;
    const [fetching, setFetching] = React.useState(false);
    const mutate = React.useCallback(async (variables: unknown) => {
      setFetching(true);
      try {
        return await actionMocks.authoredMutation(variables);
      } finally {
        setFetching(false);
      }
    }, []);
    return [mutate, { fetching, error: null }];
  },
  useAuthoredQuery: (_document: unknown, variables: Record<string, unknown>) => {
    actionMocks.queryVariables = variables;
    return {
      data: { channel_pairing: {
        state: "AWAITING_SCAN", qr: "data:image/png;base64,qr", message: "",
        can_skip: false, account_label: "", duplicate_channel_name: "",
      } },
      error: null,
    };
  },
}));

vi.mock("@angee/ui", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@angee/ui")>()),
  useActionResultMutation: () => [vi.fn(), { fetching: false, error: null }],
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
    actionMocks.queryVariables = null;
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
    expect(actionMocks.queryVariables).toEqual({ id: "chn_1" });
    expect(screen.getByText("channel.signal.scan")).toBeTruthy();
  });

  test("shows Connect loading until Signal creation settles", async () => {
    let finish!: (value: { connect_signal_channel: { id: string } }) => void;
    actionMocks.authoredMutation.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    render(<ActionMenu label="Connect"><ConnectSignalChannelAction /></ActionMenu>);
    const trigger = screen.getByRole<HTMLButtonElement>("button", { name: "Connect" });
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("menuitem", { name: "channel.signal.button" }));
    await waitFor(() => expect(trigger.getAttribute("aria-busy")).toBe("true"));
    expect(trigger.disabled).toBe(true);
    expect(screen.queryByRole("dialog")).toBeNull();
    await act(async () => { finish({ connect_signal_channel: { id: "chn_1" } }); });
    expect(await screen.findByRole("dialog", { name: "Link this channel" })).toBeTruthy();
    await waitFor(() => expect(trigger.getAttribute("aria-busy")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(trigger.disabled).toBe(false);
  });

  test("opens pairing from a real Connect menu item and restores focus after dismissal", async () => {
    render(<ActionMenu label="Connect"><ConnectSignalChannelAction /></ActionMenu>);
    const trigger = screen.getByRole("button", { name: "Connect" });
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("menuitem", { name: "channel.signal.button" }));
    expect(await screen.findByRole("dialog", { name: "Link this channel" })).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
    expect(screen.getByRole("dialog", { name: "Link this channel" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(document.activeElement).toBe(trigger));
    expect(actionMocks.authoredMutation).toHaveBeenCalledWith({});
  });
});
