from pathlib import Path

p = Path('src/api/booklore_client.py')
text = p.read_text()

# Mirror Grimmory's own web-reader CBX payload: both book-level cbxProgress and
# primary-file fileProgress must advance together.
start = text.index("        if book_type == 'CBX':\n", text.index('        payload_variants = []'))
end = text.index("        elif book_type in ('EPUB', 'PDF')", start)
new_block = '''        if book_type == 'CBX':
            cbx_payload = {
                "bookId": book_id,
                "cbxProgress": {
                    "page": cbx_page or 1,
                    "percentage": pct_display,
                },
            }
            if book_file_id is not None:
                cbx_payload["fileProgress"] = {
                    "bookFileId": self._to_optional_int(book_file_id) or book_file_id,
                    "positionData": str(cbx_page or 1),
                    "progressPercent": pct_display,
                }
                payload_variants.append(("cbxProgress+fileProgress", cbx_payload))
            else:
                payload_variants.append(("cbxProgress", cbx_payload))
'''
text = text[:start] + new_block + text[end:]

# Verify CBX writes after the HTTP success. This catches a 204 response that did
# not actually move the reader-visible page/percentage.
verify_anchor = '            logger.info(f"Grimmory: {safe_filename} -> {pct_display:.1f}%")\n'
verify_pos = text.index(verify_anchor, text.index('            # Verify EPUB writes'))
cbx_verify = '''            if book_type == 'CBX':
                time.sleep(0.25)
                verified = self.get_progress_rich(ebook_filename)
                if isinstance(verified, dict):
                    verified_pct = verified.get('pct')
                    verified_page = self._to_optional_int(verified.get('page'))
                    logger.debug(
                        "Grimmory CBX verify comparison: file=%s book_id=%s variant=%s expected_pct=%.2f%% observed_pct=%s expected_page=%s observed_page=%s",
                        safe_filename,
                        book_id,
                        variant_name,
                        pct_display,
                        f"{float(verified_pct) * 100.0:.2f}%" if verified_pct is not None else "None",
                        cbx_page or 1,
                        verified_page,
                    )
                    pct_mismatch = verified_pct is not None and abs(float(verified_pct) - float(percentage)) > 0.005
                    page_mismatch = verified_page is not None and verified_page != (cbx_page or 1)
                    if pct_mismatch or page_mismatch:
                        logger.warning(
                            "Grimmory CBX write did not persist target for %s (variant=%s, expected=%.2f%% page=%s, observed=%s page=%s)",
                            safe_filename,
                            variant_name,
                            pct_display,
                            cbx_page or 1,
                            f"{float(verified_pct) * 100.0:.2f}%" if verified_pct is not None else "None",
                            verified_page,
                        )
                        last_status = f"verify_cbx_mismatch:{verified_page}:{verified_pct}"
                        continue

'''
text = text[:verify_pos] + cbx_verify + text[verify_pos:]

# Keep in-memory book cache in sync with both fixed-page fields.
old_cache = "                            cached['cbxProgress']['percentage'] = pct_display\n"
cache_pos = text.index(old_cache, text.index("elif book_type == 'CBX':", verify_pos))
text = (
    text[:cache_pos]
    + "                            cached['cbxProgress']['page'] = cbx_page or 1\n"
    + old_cache
    + text[cache_pos + len(old_cache):]
)

p.write_text(text)
