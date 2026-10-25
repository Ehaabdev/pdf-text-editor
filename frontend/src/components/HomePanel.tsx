import type { DocInfo } from "../api";
import { ADD_TEXT, BOXES, CHANGES, COMPARE, Icon, MAGNIFIER, PICTURE } from "./Icon";

type Props = {
  doc: DocInfo;
  placing: boolean;
  showText: boolean;
  magnifier: boolean;
  onToggleMagnifier: () => void;
  comparing: boolean;
  onAddText: () => void;
  onAddPicture: () => void;
  onToggleText: () => void;
  onShowChanges: () => void;
  onCompare: () => void;
};

/** The side panel when nothing is selected (wide screens): how to start, the tools, the changes. */
export default function HomePanel({ doc, placing, showText, magnifier, comparing, onAddText, onAddPicture, onToggleText, onToggleMagnifier, onShowChanges, onCompare }: Props) {
  const applied = doc.history.filter((c) => c.applied).length;
  const undone = doc.history.length - applied;

  return (
    <section className="panel home-panel" aria-label="Tools">
      {comparing ? (
        <>
          <h2 className="panel-title">Comparing with the original</h2>
          <p className="panel-lede">
            Switch between the original and your version above the pages. Every change is outlined. Editing is off
            while you compare.
          </p>
          <div className="panel-actions">
            <button className="btn btn-primary" onClick={onCompare}>
              Done comparing
            </button>
          </div>
        </>
      ) : (
        <>
          <h2 className="panel-title">Edit this PDF</h2>
          <p className="panel-lede">
            Click a line on the page to change it, add a line below or above it, or delete it. Shift-click to pick several
            lines. Click a picture to replace, move or delete it.
          </p>

          <div className="tools">
            <button className="tool" aria-pressed={placing} onClick={onAddText}>
              <Icon d={ADD_TEXT} />
              <span className="tool-text">
                <span className="tool-name">Add text</span>
                <span className="tool-note">Click where you want it on the page.</span>
              </span>
            </button>
            <button className="tool" onClick={onAddPicture}>
              <Icon d={PICTURE} />
              <span className="tool-text">
                <span className="tool-name">Add picture</span>
                <span className="tool-note">Choose a picture, then drag it into place.</span>
              </span>
            </button>
            <button className="tool" aria-pressed={showText} onClick={onToggleText}>
              <Icon d={BOXES} />
              <span className="tool-text">
                <span className="tool-name">Show editable text</span>
                <span className="tool-note">Outline every line on the pages.</span>
              </span>
              <span className="switch" aria-hidden="true" />
            </button>
            <button className="tool" aria-pressed={magnifier} onClick={onToggleMagnifier}>
              <Icon d={MAGNIFIER} />
              <span className="tool-text">
                <span className="tool-name">Magnifier</span>
                <span className="tool-note">See the print up close as you move the mouse. Key: M</span>
              </span>
              <span className="switch" aria-hidden="true" />
            </button>
          </div>
        </>
      )}

      <div className="panel-section">
        <h3 className="panel-subtitle">Changes</h3>
        {doc.history.length === 0 ? (
          <p className="panel-note">None yet. Each change you make is listed, so you can check it and undo it.</p>
        ) : (
          <>
            <p className="panel-note">
              {applied === 0 ? "None applied" : applied === 1 ? "1 change" : `${applied} changes`}
              {undone > 0 ? `, ${undone} undone` : ""}.
            </p>
            <div className="panel-actions">
              <button className="btn btn-secondary" onClick={onShowChanges}>
                <Icon d={CHANGES} />
                See changes
              </button>
              {doc.version > 0 && !comparing && (
                <button className="btn btn-ghost" onClick={onCompare}>
                  <Icon d={COMPARE} />
                  Compare
                </button>
              )}
            </div>
          </>
        )}
      </div>

      <footer className="panel-foot">
        <p>Your file is deleted an hour after you last use it.</p>
      </footer>
    </section>
  );
}
