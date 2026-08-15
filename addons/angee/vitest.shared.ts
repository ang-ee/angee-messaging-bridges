import { fileURLToPath } from "node:url";

export const gqlAlias = [
  {
    find: /^@angee\/gql\//,
    replacement: fileURLToPath(
      new URL("../../../angee-django/examples/notes-angee/runtime/gql/", import.meta.url),
    ),
  },
];
