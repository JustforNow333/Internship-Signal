import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import UploadPanel from "../components/UploadPanel.jsx";


describe("UploadPanel", () => {
  const droppedFile = () => new File(
    ["Company,Title\nAcme,Intern"],
    "jobs.csv",
    { type: "text/csv" }
  );

  it("ignores drag-and-drop uploads while an ingestion request is busy", () => {
    const onIngestFile = vi.fn();
    render(
      <UploadPanel
        onIngestFile={onIngestFile}
        onIngestText={vi.fn()}
        onLoadSample={vi.fn()}
        busy
      />
    );
    const dropzone = screen.getByText(/Drop a CSV here/).closest(".dropzone");

    fireEvent.drop(dropzone, { dataTransfer: { files: [droppedFile()] } });

    expect(onIngestFile).not.toHaveBeenCalled();
  });

  it("accepts a drag-and-drop upload when idle", () => {
    const onIngestFile = vi.fn();
    render(
      <UploadPanel
        onIngestFile={onIngestFile}
        onIngestText={vi.fn()}
        onLoadSample={vi.fn()}
        busy={false}
      />
    );
    const dropzone = screen.getByText(/Drop a CSV here/).closest(".dropzone");
    const file = droppedFile();

    fireEvent.drop(dropzone, { dataTransfer: { files: [file] } });

    expect(onIngestFile).toHaveBeenCalledWith(file);
  });
});
