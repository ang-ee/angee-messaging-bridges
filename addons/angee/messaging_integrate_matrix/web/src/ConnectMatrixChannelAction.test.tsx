// @vitest-environment happy-dom

import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const actionMocks = vi.hoisted(() => ({
  props: null as Record<string, unknown> | null,
}));

vi.mock("@angee/messaging", () => ({
  ConnectChannelAction: (props: Record<string, unknown>) => {
    actionMocks.props = props;
    return <button type="button">connect</button>;
  },
}));

import { ConnectMatrixChannelAction } from "./ConnectMatrixChannelAction";

describe("ConnectMatrixChannelAction", () => {
  afterEach(cleanup);

  beforeEach(() => {
    actionMocks.props = null;
  });

  test("declares Matrix credentials and recovery pairing", () => {
    render(<ConnectMatrixChannelAction />);
    const fields = actionMocks.props?.fields as (
      t: (key: string) => string,
    ) => readonly { name: string; widget?: string }[];
    const parseValues = actionMocks.props?.parseValues as (
      values: Record<string, unknown>,
    ) => unknown;

    expect(actionMocks.props).toMatchObject({
      kind: "pairing",
      i18nPrefix: "channel.matrix",
      resultField: "connect_matrix_channel",
      instructionKey: "channel.matrix.recovery",
    });
    expect(fields((key) => key)).toMatchObject([
      { name: "homeserver" },
      { name: "username" },
      { name: "password", widget: "password" },
    ]);
    expect(
      parseValues({
        homeserver: " https://matrix.example.com/ ",
        username: " @ada:example.com ",
        password: " matrix-password ",
      }),
    ).toEqual({
      homeserver: "https://matrix.example.com/",
      username: "@ada:example.com",
      password: " matrix-password ",
    });
  });
});
