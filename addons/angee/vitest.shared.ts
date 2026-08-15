import { fileURLToPath } from "node:url";

export const gqlAlias = [
  {
    find: /^@angee\/gql\//,
    replacement: fileURLToPath(
      new URL("../../../../../runtime/gql/", import.meta.url),
    ),
  },
];
