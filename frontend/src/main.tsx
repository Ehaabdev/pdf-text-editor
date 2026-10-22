import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/readex-pro";
import "./styles.css";
import App from "./App";
import { startPointerLight } from "./pointerLight";

startPointerLight();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
