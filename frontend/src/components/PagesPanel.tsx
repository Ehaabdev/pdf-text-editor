import type { DocInfo } from "../api";
import { CLOSE, Icon } from "./Icon";
import Thumbnails, { type PageEdits } from "./Thumbnails";

type Props = {
  doc: DocInfo;
  version?: number;
  current: number;
  onJump: (page: number) => void;
  onClose: () => void;
  edit?: PageEdits;
};

/** Every page as a small picture, to jump to (where there's no room for the list beside the pages). */
export default function PagesPanel({ doc, version, current, onJump, onClose, edit }: Props) {
  return (
    <section className="panel pages-panel" aria-label="Pages">
      <div className="panel-head">
        <h2 className="panel-title">
          {doc.pageCount} {doc.pageCount === 1 ? "page" : "pages"}
        </h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <Icon d={CLOSE} />
        </button>
      </div>
      <Thumbnails doc={doc} version={version} current={current} onJump={onJump} className="thumbs--grid" edit={edit} />
    </section>
  );
}
