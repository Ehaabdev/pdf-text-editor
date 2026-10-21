// Set per deployment in .env / .env.local (VITE_APP_NAME, VITE_SOURCE_URL).
export const APP_NAME: string = import.meta.env.VITE_APP_NAME || "PDF Editor";

// The code is AGPL-licensed, so the public site must link to its source.
export const SOURCE_URL: string = import.meta.env.VITE_SOURCE_URL || "https://github.com/Ehaabdev/pdf-text-editor";
