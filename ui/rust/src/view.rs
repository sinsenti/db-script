//! Rendering is pure: only cached application data is consumed here.
use crate::{app::*, data::*};
use ratatui::{prelude::*, widgets::*};
use unicode_width::UnicodeWidthStr;
const BG: Color = Color::Rgb(15, 22, 34);
const PANEL: Color = Color::Rgb(23, 33, 48);
const ACCENT: Color = Color::Rgb(71, 211, 183);
const MUTED: Color = Color::Rgb(133, 153, 176);
fn block(title: impl Into<Line<'static>>, focused: bool) -> Block<'static> {
    Block::bordered()
        .title(title)
        .border_style(Style::default().fg(if focused {
            ACCENT
        } else {
            Color::Rgb(56, 75, 96)
        }))
        .style(Style::default().bg(PANEL))
}
fn centered(area: Rect, w: u16, h: u16) -> Rect {
    let w = w.min(area.width.saturating_sub(2));
    let h = h.min(area.height.saturating_sub(2));
    Rect::new(
        area.x + (area.width - w) / 2,
        area.y + (area.height - h) / 2,
        w,
        h,
    )
}
pub fn draw(f: &mut Frame, a: &App) {
    let area = f.area();
    f.render_widget(
        Block::default().style(Style::default().bg(BG).fg(Color::Gray)),
        area,
    );
    if area.width < 35 || area.height < 10 {
        f.render_widget(
            Paragraph::new("Resize terminal to at least 35×10. q quits.")
                .wrap(Wrap { trim: false }),
            area,
        );
        return;
    }
    let rows = Layout::vertical([
        Constraint::Length(2),
        Constraint::Min(1),
        Constraint::Length(2),
    ])
    .split(area);
    let top = Line::from(vec![
        Span::styled(
            " DBCLI ",
            Style::default().fg(Color::Black).bg(ACCENT).bold(),
        ),
        Span::raw(format!(
            " {} / {} · {} ",
            a.config.engine,
            a.config.backend,
            if a.read_only() {
                "READ ONLY"
            } else {
                "WRITABLE"
            }
        )),
        Span::styled(
            if a.busy { " ◌ BUSY" } else { " ● READY" },
            Style::default().fg(if a.busy { Color::Yellow } else { ACCENT }),
        ),
    ]);
    f.render_widget(Paragraph::new(top), rows[0]);
    let cols = Layout::horizontal([
        Constraint::Length(if area.width < 70 { 18 } else { 25 }),
        Constraint::Min(10),
    ])
    .split(rows[1]);
    let names = a.table_names();
    let list = List::new(names.iter().map(|t| ListItem::new(t.clone())))
        .block(block(
            format!(" Tables ({}) / search ", names.len()),
            a.focus == Focus::Sidebar,
        ))
        .highlight_style(Style::default().bg(Color::Rgb(41, 70, 84)).fg(ACCENT))
        .highlight_symbol("› ");
    let mut state =
        ListState::default().with_selected(Some(a.table_cursor.min(names.len().saturating_sub(1))));
    f.render_stateful_widget(list, cols[0], &mut state);
    let main = Layout::vertical([Constraint::Length(3), Constraint::Min(1)]).split(cols[1]);
    let index = match a.view {
        View::Data => 0,
        View::Sql => 1,
        View::Schema => 2,
        View::Inspect => 3,
    };
    f.render_widget(
        Tabs::new(["1 Data", "2 SQL", "3 Schema", "4 Inspect"])
            .select(index)
            .highlight_style(Style::default().fg(ACCENT).bold())
            .block(block(" Views · 1/2/3/4 ", false)),
        main[0],
    );
    match a.view {
        View::Data => grid(f, a, main[1]),
        View::Sql => f.render_widget(
            Paragraph::new(if a.draft.is_empty() {
                "Press : to compose SQL. Ctrl+R runs a bounded read-only query."
            } else {
                &a.draft
            })
            .block(block(" SQL · : edit / Ctrl+R run ", true))
            .wrap(Wrap { trim: false }),
            main[1],
        ),
        View::Schema => result(f, &a.meta.schema, " Schema ", a.scroll, main[1]),
        View::Inspect => result(
            f,
            &a.inspector,
            " Indexes · constraints · triggers ",
            a.scroll,
            main[1],
        ),
    }
    f.render_widget(
        Paragraph::new(vec![
            Line::styled(
                a.status.clone(),
                Style::default().fg(if a.status.contains("Error") {
                    Color::LightRed
                } else {
                    ACCENT
                }),
            ),
            Line::styled(
                format!(" {} · Tab tables/rows  D diagram  / filter  : SQL  a/e/E edit  dd delete  x/X export  ? help  q back ",
                    if a.focus == Focus::Sidebar { "Tables focused" } else { "Rows focused" }),
                Style::default().fg(MUTED),
            ),
        ]),
        rows[2],
    );
    if let Some(m) = &a.modal {
        modal(f, a, m, area);
    }
}
fn grid(f: &mut Frame, a: &App, area: Rect) {
    let visible = viewport(a, area.width);
    let mut widths = vec![Constraint::Length(2)];
    widths.extend(visible.iter().map(|(_, _, w)| Constraint::Length(*w)));
    let mut headers = vec![Cell::from(" ")];
    headers.extend(visible.iter().map(|(i, h, _)| {
        Cell::from(format!(
            "{}{}",
            h,
            if a.spec.sort.as_ref().is_some_and(|(c, _)| c == *h) {
                if a.spec.sort.as_ref().unwrap().1 == "ASC" {
                    " ↑"
                } else {
                    " ↓"
                }
            } else {
                ""
            }
        ))
        .style(if *i == a.column {
            Style::default().fg(ACCENT).bold()
        } else {
            Style::default().fg(MUTED).bold()
        })
    }));
    let rows = a.page.rows.iter().map(|row| {
        let object = object(&a.page.headers, row);
        let id = identity(&object, &a.meta.keys).unwrap_or_else(|_| object.to_string());
        let mut cells = vec![Cell::from(if a.marks.contains_key(&id) {
            "●"
        } else {
            " "
        })
        .style(Style::default().fg(ACCENT))];
        cells.extend(visible.iter().map(|(i, _, _)| {
            Cell::from(
                row.get(*i)
                    .map(text)
                    .unwrap_or_default()
                    .replace('\n', " ↵ "),
            )
        }));
        Row::new(cells).style(Style::default().fg(Color::Gray))
    });
    let title = format!(
        " {} · {}–{} / {} · {} marks {} ",
        a.spec.table,
        if a.page.rows.is_empty() {
            0
        } else {
            a.spec.offset + 1
        },
        a.spec.offset + a.page.rows.len(),
        a.total,
        a.marks.len(),
        if a.spec.filter.is_empty() {
            String::new()
        } else {
            format!("filter: {}", a.spec.filter)
        }
    );
    let table = Table::new(rows, widths)
        .header(Row::new(headers).height(1).bottom_margin(1))
        .row_highlight_style(Style::default().bg(Color::Rgb(39, 64, 83)))
        .block(block(title, a.focus == Focus::Grid))
        .column_spacing(2);
    let mut state = TableState::default().with_selected(Some(a.cursor));
    f.render_stateful_widget(table, area, &mut state);
}
fn viewport(a: &App, width: u16) -> Vec<(usize, &String, u16)> {
    let columns = a
        .page
        .headers
        .iter()
        .enumerate()
        .filter(|(_, h)| !a.hidden.contains(*h))
        .collect::<Vec<_>>();
    let focus = columns
        .iter()
        .position(|(i, _)| *i == a.column)
        .unwrap_or(0);
    let budget = width.saturating_sub(6);
    let fit = |start: usize| {
        let mut left = budget;
        let mut result = vec![];
        for (i, h) in columns.iter().skip(start) {
            let natural = column_width(a, h) as u16;
            if natural > left && !result.is_empty() {
                break;
            }
            let w = natural.min(left);
            if w == 0 {
                break;
            }
            result.push((*i, *h, w));
            left = left.saturating_sub(w + 2);
        }
        result
    };
    for start in 0..=focus {
        let result = fit(start);
        if result.iter().any(|(i, _, _)| *i == a.column) || columns.is_empty() {
            return result;
        }
    }
    fit(focus)
}

fn column_width(a: &App, h: &str) -> usize {
    let i = a.page.headers.iter().position(|c| c == h).unwrap_or(0);
    a.page
        .rows
        .iter()
        .filter_map(|r| r.get(i))
        .take(30)
        .map(|v| UnicodeWidthStr::width(text(v).as_str()))
        .max()
        .unwrap_or(0)
        .max(UnicodeWidthStr::width(h) + 2)
        .clamp(10, 32)
}
fn result(f: &mut Frame, p: &Page, title: &str, scroll: u16, area: Rect) {
    let border = block(title.to_string(), true).padding(Padding::horizontal(1));
    let inner = border.inner(area);
    if p.headers.is_empty() || inner.width == 0 {
        f.render_widget(Paragraph::new("No metadata available").block(border), area);
        return;
    }
    let headers = p
        .headers
        .iter()
        .map(|h| match h.as_str() {
            "Column Name" => "Column".to_string(),
            "NotNull" => "Not null".to_string(),
            _ => h.clone(),
        })
        .collect::<Vec<_>>();
    let spacing = if inner.width >= 75 { 2 } else { 1 };
    let budget = (inner.width as usize).saturating_sub(spacing * headers.len().saturating_sub(1));
    let natural = headers
        .iter()
        .enumerate()
        .map(|(i, h)| {
            let value_width = p
                .rows
                .iter()
                .filter_map(|r| r.get(i))
                .map(|v| {
                    let value = text(v);
                    value.lines().map(UnicodeWidthStr::width).max().unwrap_or(0)
                })
                .max()
                .unwrap_or(0);
            UnicodeWidthStr::width(h.as_str())
                .max(value_width)
                .clamp(1, 48)
        })
        .collect::<Vec<_>>();
    let mut widths = headers
        .iter()
        .map(|h| UnicodeWidthStr::width(h.as_str()).clamp(1, 12))
        .collect::<Vec<_>>();
    // Prefer readable headers, then share remaining space between values that need it.
    while widths.iter().sum::<usize>() > budget {
        let Some((i, _)) = widths
            .iter()
            .enumerate()
            .filter(|(_, w)| **w > 1)
            .max_by_key(|(_, w)| **w)
        else {
            break;
        };
        widths[i] -= 1;
    }
    let mut spare = budget.saturating_sub(widths.iter().sum());
    while spare > 0 {
        let mut changed = false;
        for (i, width) in widths.iter_mut().enumerate() {
            if *width < natural[i] && spare > 0 {
                *width += 1;
                spare -= 1;
                changed = true;
            }
        }
        if !changed {
            break;
        }
    }
    let header_cells = headers
        .iter()
        .zip(&widths)
        .map(|(h, w)| {
            let lines = wrapped_lines(h, *w);
            Cell::from(Text::from(
                lines.into_iter().map(Line::from).collect::<Vec<_>>(),
            ))
        })
        .collect::<Vec<_>>();
    let header_height = headers
        .iter()
        .zip(&widths)
        .map(|(h, w)| wrapped_lines(h, *w).len())
        .max()
        .unwrap_or(1) as u16;
    let start = (scroll as usize).min(p.rows.len().saturating_sub(1));
    let rows = p
        .rows
        .iter()
        .enumerate()
        .skip(start)
        .take(inner.height as usize)
        .map(|(n, row)| {
            let mut height = 1;
            let cells = p
                .headers
                .iter()
                .enumerate()
                .zip(&widths)
                .map(|((i, h), width)| {
                    let value = row
                        .get(i)
                        .map(text)
                        .unwrap_or_default()
                        .replace('\t', "  ")
                        .replace('\r', "");
                    let lines = wrapped_lines(&value, *width);
                    height = height.max(lines.len().min(u16::MAX as usize) as u16);
                    let alignment = if h == "CID" {
                        Alignment::Right
                    } else if h == "PK" || h == "NotNull" {
                        Alignment::Center
                    } else {
                        Alignment::Left
                    };
                    let style = match value.as_str() {
                        "YES" => Style::default().fg(ACCENT),
                        "NO" | "NULL" => Style::default().fg(MUTED),
                        _ => Style::default().fg(Color::Gray),
                    };
                    Cell::from(
                        Text::from(lines.into_iter().map(Line::from).collect::<Vec<_>>())
                            .alignment(alignment),
                    )
                    .style(style)
                })
                .collect::<Vec<_>>();
            Row::new(cells)
                .height(height)
                .style(Style::default().bg(if n % 2 == 0 {
                    PANEL
                } else {
                    Color::Rgb(19, 29, 43)
                }))
        });
    let border = border.title_bottom(Line::styled(
        format!(" {} entries · j/k scroll ", p.rows.len()),
        Style::default().fg(MUTED),
    ));
    f.render_widget(
        Table::new(rows, widths.iter().map(|w| Constraint::Length(*w as u16)))
            .header(
                Row::new(header_cells)
                    .height(header_height)
                    .bottom_margin(1)
                    .style(Style::default().fg(ACCENT).bold()),
            )
            .column_spacing(spacing as u16)
            .block(border),
        area,
    );
}

fn help_rect(area: Rect) -> Rect {
    centered(
        area,
        area.width.saturating_sub(8).min(100),
        area.height.saturating_sub(4),
    )
}
fn help_lines(width: usize) -> Vec<Line<'static>> {
    let width = width.max(1);
    let key_width = 16.min(width.saturating_sub(8)).max(1);
    let description_width = width.saturating_sub(key_width + 2).max(1);
    let mut lines = Vec::new();
    for line in HELP.lines() {
        if let Some((key, description)) = line.split_once('\t') {
            let keys = wrapped_lines(key, key_width);
            let descriptions = wrapped_lines(description, description_width);
            for n in 0..keys.len().max(descriptions.len()) {
                let key = keys.get(n).cloned().unwrap_or_default();
                let padding =
                    " ".repeat(key_width.saturating_sub(UnicodeWidthStr::width(key.as_str())) + 2);
                lines.push(Line::from(vec![
                    Span::styled(key, Style::default().fg(Color::Rgb(244, 203, 126)).bold()),
                    Span::raw(padding),
                    Span::styled(
                        descriptions.get(n).cloned().unwrap_or_default(),
                        Style::default().fg(Color::Gray),
                    ),
                ]));
            }
        } else if line.is_empty() {
            lines.push(Line::raw(""));
        } else if line == line.to_uppercase() {
            lines.extend(
                wrapped_lines(line, width)
                    .into_iter()
                    .map(|l| Line::styled(l, Style::default().fg(ACCENT).bold())),
            );
        } else {
            lines.extend(
                wrapped_lines(line, width)
                    .into_iter()
                    .map(|l| Line::styled(l, Style::default().fg(MUTED))),
            );
        }
    }
    lines
}
pub fn help_dimensions(width: u16, height: u16) -> (usize, usize) {
    let rect = help_rect(Rect::new(0, 0, width, height));
    (
        help_lines(rect.width.saturating_sub(4) as usize).len(),
        rect.height.saturating_sub(2) as usize,
    )
}
fn help(f: &mut Frame, area: Rect, scroll: usize) {
    let rect = help_rect(area);
    let border = block(" Keyboard shortcuts ", true).padding(Padding::horizontal(1));
    let lines = help_lines(border.inner(rect).width as usize);
    let visible = border.inner(rect).height as usize;
    let scroll = scroll.min(lines.len().saturating_sub(visible));
    let end = (scroll + visible).min(lines.len());
    let border = border.title_bottom(Line::styled(
        format!(" {}–{} / {} · Esc to close ", scroll + 1, end, lines.len()),
        Style::default().fg(MUTED),
    ));
    f.render_widget(Clear, rect);
    f.render_widget(
        Paragraph::new(
            lines
                .into_iter()
                .skip(scroll)
                .take(visible)
                .collect::<Vec<_>>(),
        )
        .block(border),
        rect,
    );
}
pub fn wrapped_lines(text: &str, width: usize) -> Vec<String> {
    use unicode_segmentation::UnicodeSegmentation;
    let width = width.max(1);
    let mut lines = vec![];
    for line in text.split('\n') {
        let mut current = String::new();
        let mut used = 0;
        for grapheme in line.graphemes(true) {
            let w = UnicodeWidthStr::width(grapheme);
            if used + w > width && !current.is_empty() {
                lines.push(std::mem::take(&mut current));
                used = 0;
            }
            current.push_str(grapheme);
            used += w;
        }
        lines.push(current);
    }
    lines
}

fn filter_input(f: &mut Frame, a: &App, text: &str, cursor: usize, rect: Rect) {
    let border = block(" Filter · Ctrl+R apply · Ctrl+G editor ", true).title_bottom(Line::styled(
        " Esc hides suggestions / closes prompt ",
        Style::default().fg(MUTED),
    ));
    let inner = border.inner(rect);
    f.render_widget(border, rect);
    let count = a.filter_suggestions.as_ref().map_or(0, |s| s.items.len());
    let popup_height = if count == 0 {
        0
    } else {
        (count.min(8) as u16 + 2).min(inner.height.saturating_sub(3))
    };
    let parts =
        Layout::vertical([Constraint::Min(1), Constraint::Length(popup_height)]).split(inner);
    let before: String = text.chars().take(cursor).collect();
    let lines = wrapped_lines(&before, parts[0].width as usize);
    let last_width = lines
        .last()
        .map_or(0, |line| UnicodeWidthStr::width(line.as_str()));
    let boundary = parts[0].width > 0 && last_width >= parts[0].width as usize;
    let cursor_y = lines.len().saturating_sub(1) + usize::from(boundary);
    let cursor_x = if boundary { 0 } else { last_width };
    let scroll = cursor_y.saturating_sub(parts[0].height.saturating_sub(1) as usize);
    f.render_widget(
        Paragraph::new(wrapped_lines(text, parts[0].width as usize).join("\n"))
            .scroll((scroll as u16, 0)),
        parts[0],
    );
    if let Some(suggestions) = &a.filter_suggestions {
        if popup_height > 0 {
            let list = List::new(suggestions.items.iter().map(|item| {
                ListItem::new(Line::from(vec![
                    Span::styled(format!("{:<13} ", item.kind), Style::default().fg(MUTED)),
                    Span::raw(item.label.clone()),
                ]))
            }))
            .block(block(
                " Suggestions · ↑/↓ choose · Tab/Enter accept ",
                false,
            ))
            .highlight_symbol("› ")
            .highlight_style(
                Style::default()
                    .fg(ACCENT)
                    .bg(Color::Rgb(39, 64, 83))
                    .bold(),
            );
            f.render_stateful_widget(
                list,
                parts[1],
                &mut ListState::default().with_selected(Some(suggestions.selected)),
            );
        }
    }
    if parts[0].width > 0 && parts[0].height > 0 {
        f.set_cursor_position((
            parts[0].x + (cursor_x as u16).min(parts[0].width - 1),
            parts[0].y + ((cursor_y - scroll) as u16).min(parts[0].height - 1),
        ));
    }
}

fn modal(f: &mut Frame, a: &App, m: &Modal, area: Rect) {
    if let Modal::Help { scroll } = m {
        help(f, area, *scroll);
        return;
    }
    let rect = if matches!(m, Modal::BulkConfirm { .. }) {
        centered(area, area.width.saturating_sub(8).min(90), 8)
    } else {
        centered(
            area,
            area.width.saturating_sub(8).min(110),
            area.height.saturating_sub(4),
        )
    };
    f.render_widget(Clear, rect);
    match m {
        Modal::Help { .. } => unreachable!("help is rendered separately"),
        Modal::Text {
            title,
            text,
            scroll,
            ..
        } => f.render_widget(
            Paragraph::new(
                wrapped_lines(text, rect.width.saturating_sub(2) as usize)
                    .into_iter()
                    .skip(*scroll)
                    .collect::<Vec<_>>()
                    .join("\n"),
            )
            .block(block(title.clone(), true)),
            rect,
        ),
        Modal::Choices {
            title,
            items,
            selected,
            ..
        } => {
            let list = List::new(items.iter().map(|i| ListItem::new(i.clone())))
                .block(block(format!(" {title} · j/k Enter · Esc cancel "), true))
                .highlight_symbol("› ")
                .highlight_style(Style::default().fg(ACCENT).bg(Color::Rgb(39, 64, 83)));
            f.render_stateful_widget(
                list,
                rect,
                &mut ListState::default().with_selected(Some(*selected)),
            );
        }
        Modal::BulkConfirm {
            actions,
            confirmation,
        } => {
            let deletes = actions.iter().any(|a| a["kind"] == "delete");
            let text = format!(
                "{}\n\nDiff reviewed in Neovim. No row data is rendered here.\n{}\nEsc/q cancels",
                crate::bulk_editor::summary(actions),
                if deletes {
                    format!("Type DELETE then y to apply [{confirmation}]")
                } else {
                    "y applies one atomic transaction".into()
                }
            );
            f.render_widget(
                Paragraph::new(text).block(block(" Bulk count confirmation ", true)),
                rect,
            );
        }
        Modal::Preview {
            actions,
            scroll,
            confirmation,
        } => {
            let delete = actions.iter().any(|a| a["kind"] == "delete");
            let title = format!(
                " Atomic preview · {} actions · {} ",
                actions.len(),
                if delete {
                    format!("type DELETE then y [{confirmation}]")
                } else {
                    "y apply / Esc cancel".into()
                }
            );
            f.render_widget(
                Paragraph::new(
                    wrapped_lines(
                        &serde_json::to_string_pretty(actions).unwrap(),
                        rect.width.saturating_sub(2) as usize,
                    )
                    .into_iter()
                    .skip(*scroll)
                    .collect::<Vec<_>>()
                    .join("\n"),
                )
                .block(block(title, true)),
                rect,
            );
        }
        Modal::Input { kind, text, cursor } => {
            if matches!(kind, Edit::Filter) {
                filter_input(f, a, text, *cursor, rect);
                return;
            }
            let label = match kind {
                Edit::Filter => "Filter · Enter apply",
                Edit::Search => "Search tables · Enter apply",
                Edit::Sql => "SQL · Ctrl+R run",
                Edit::Row { .. } => "Edit JSON · Ctrl+R preview",
                Edit::Insert => "Insert JSON object/array · Ctrl+R preview",
                Edit::Bulk { .. } => "Atomic bulk JSON · Ctrl+R preview",
                Edit::Export { .. } => "Export path · Enter save",
                Edit::Palette => "Command palette · Enter run",
            };
            let title = format!(" {label} · Ctrl+G external editor · Esc cancel ");
            let before = text.chars().take(*cursor).collect::<String>();
            let line = before.chars().filter(|c| *c == '\n').count();
            let x = UnicodeWidthStr::width(before.rsplit('\n').next().unwrap_or(""));
            let scroll = line.saturating_sub(rect.height.saturating_sub(5) as usize);
            let content = if matches!(kind, Edit::Palette) {
                format!(
                    "{text}\n\nCommands · type a name or Tab complete, Enter run\n{}",
                    crate::app::COMMANDS
                        .iter()
                        .filter(|c| crate::app::fuzzy(text, c))
                        .cloned()
                        .collect::<Vec<_>>()
                        .join("\n")
                )
            } else {
                text.clone()
            };
            f.render_widget(
                Paragraph::new(content)
                    .block(block(title, true))
                    .wrap(Wrap { trim: false })
                    .scroll((scroll as u16, 0)),
                rect,
            );
            f.set_cursor_position((
                rect.x + 1 + (x as u16).min(rect.width.saturating_sub(3)),
                rect.y + 1 + (line - scroll) as u16,
            ));
        }
    }
    let _ = a;
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn large_previews_are_not_limited_to_u16_offsets() {
        let text = "long🙂value\n".repeat(70000);
        let lines = wrapped_lines(&text, 8);
        assert!(lines.len() > 65535);
        assert!(!lines[69999].is_empty());
        assert_eq!(
            wrapped_lines("e\u{301}🙂混合", 4),
            vec!["e\u{301}🙂", "混合"]
        );
    }
    #[test]
    fn wide_tables_use_natural_width_viewport() {
        let mut a = App::new(Config::default());
        a.page.headers = (0..18).map(|i| format!("column_{i}")).collect();
        a.page.rows = vec![(0..18).map(|_| serde_json::json!("alpha")).collect()];
        let columns = viewport(&a, 85);
        assert!(columns.len() < 18);
        assert!(columns.iter().all(|(_, _, w)| *w >= 10));
        assert!(columns.iter().map(|(_, _, w)| *w + 2).sum::<u16>() <= 81);
        a.column = 17;
        let columns = viewport(&a, 85);
        assert!(columns.iter().any(|(i, _, _)| *i == 17));
    }
    #[test]
    fn renders_unicode_resizes_and_modals_without_database() {
        let mut a = App::new(Config::default());
        a.busy = false;
        a.spec.table = "unicode".into();
        a.page = Page {
            sql_nulls: vec![],
            headers: vec!["名字".into()],
            rows: vec![vec![serde_json::json!("混合🙂")]],
        };
        for (w, h) in [(120, 35), (60, 20), (30, 8)] {
            let backend = ratatui::backend::TestBackend::new(w, h);
            let mut terminal = ratatui::Terminal::new(backend).unwrap();
            terminal.draw(|f| draw(f, &a)).unwrap();
        }
        a.modal = Some(Modal::Text {
            title: "Detail".into(),
            text: "text".into(),
            scroll: 0,
            detail: true,
        });
        let backend = ratatui::backend::TestBackend::new(80, 24);
        let mut t = ratatui::Terminal::new(backend).unwrap();
        t.draw(|f| draw(f, &a)).unwrap();
    }
}
