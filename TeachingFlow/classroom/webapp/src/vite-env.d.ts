/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_MOCK?: string;
  readonly VITE_PRODUCE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
