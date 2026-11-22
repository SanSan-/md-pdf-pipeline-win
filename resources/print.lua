local function latex(block)
  return pandoc.write(pandoc.Pandoc({block}), "latex",
    pandoc.WriterOptions({wrap_text = "none"}))
end

local glyphs = {
  ["∑"] = "\\sum", ["∈"] = "\\in", ["∉"] = "\\notin",
  ["∪"] = "\\cup", ["∩"] = "\\cap", ["⇒"] = "\\Rightarrow",
  ["⇔"] = "\\Leftrightarrow", ["⋅"] = "\\cdot", ["⊕"] = "\\oplus",
  ["⊆"] = "\\subseteq", ["⌈"] = "\\lceil", ["⌉"] = "\\rceil",
  ["⌊"] = "\\lfloor", ["⌋"] = "\\rfloor", ["≪"] = "\\ll",
  ["ℓ"] = "\\ell", ["ₙ"] = "{}_n", ["∀"] = "\\forall",
  ["⩽"] = "\\leqslant", ["⩾"] = "\\geqslant", ["∗"] = "\\ast",
  ["⋯"] = "\\cdots", ["∣"] = "\\mid",
}

local function text_symbols(element)
  local value = element.text
  if not value:find("[\128-\255]") then return end
  local inlines, start = pandoc.Inlines({}), 1
  for offset, char in utf8.codes(value) do
    local symbol = utf8.char(char)
    if glyphs[symbol] then
      if start < offset then inlines:insert(pandoc.Str(value:sub(start, offset - 1))) end
      inlines:insert(pandoc.Math("InlineMath", glyphs[symbol]))
      start = offset + #symbol
    end
  end
  if start == 1 then return end
  if start <= #value then inlines:insert(pandoc.Str(value:sub(start))) end
  return inlines
end

local function math_symbols(element)
  -- В окружениях aligned, cases и matrix двойная черта завершает строку.
  -- Сохраняем её даже перед именем, похожим на старую запись команды.
  local legacy = {le = true, ge = true, ne = true, cdot = true}
  local single_line = not element.text:find("\\begin%s*{")
  element.text = element.text:gsub("(\\+)([a-zA-Z]+)", function(slashes, name)
    if slashes == "\\" and name == "lt" then return "<" end
    if slashes == "\\" and name == "gt" then return ">" end
    if slashes == "\\\\" and single_line and legacy[name] then return "\\" .. name .. " " end
    return slashes .. name
  end)
  return element
end

local function table_widths(element)
  local count = #element.colspecs
  for _, column in ipairs(element.colspecs) do
    if (column[2] or 0) > 0 then return end
  end
  for _, column in ipairs(element.colspecs) do column[2] = 0.999 / count end
  return element
end

local function inline_code(element)
  if not element.text:find("[a-z0-9][A-Z]") and not element.text:find("[./_]") then return end
  local value = latex(pandoc.Plain({element}))
  value = value:gsub("([a-z0-9])([A-Z])", "%1\\allowbreak{}%2")
               :gsub("([./])", "%1\\allowbreak{}")
               :gsub("\\_", "\\_\\allowbreak{}")
  return pandoc.RawInline("latex", value)
end

local function deep_heading(element)
  if element.level <= 3 then return end
  -- Поручаем Pandoc экранирование идентификатора и обеих версий заголовка:
  -- печатной (включая формулы) и текстовой для панели закладок.
  local heading = pandoc.Header(1, element.content, pandoc.Attr(element.identifier))
  local include = element.classes:includes("unlisted") and 0 or 1
  local macro = string.format("\\BookDeepHeading{%d}{%d}", element.level, include)
  local content = latex(heading):gsub("^\\section", function() return macro end)
  return pandoc.RawBlock("latex", content)
end

local function is_label(block)
  if block.t ~= "Para" then return false end
  local strong = false
  for _, inline in ipairs(block.content) do
    if inline.t == "Strong" then
      strong = true
    elseif inline.t ~= "Space" and inline.t ~= "SoftBreak" and
           inline.t ~= "LineBreak" and not (inline.t == "Str" and inline.text == ":") then
      return false
    end
  end
  return strong
end

local function group_headings(doc)
  local blocks, pending = pandoc.Blocks({}), pandoc.Blocks({})
  local function flush(next_block)
    if #pending == 0 then return end
    local cmd = next_block and next_block.t == "Table" and "BookTableHeadings" or "BookHeadings"
    blocks:insert(pandoc.RawBlock("latex", "\\" .. cmd .. "{"))
    blocks:extend(pending)
    blocks:insert(pandoc.RawBlock("latex", "}"))
    pending = pandoc.Blocks({})
  end
  for i, block in ipairs(doc.blocks) do
    if block.t == "Header" then
      pending:insert(block)
    elseif is_label(block) and doc.blocks[i + 1] and doc.blocks[i + 1].t ~= "Header" then
      pending:insert(block)
      pending:insert(pandoc.RawBlock("latex", "\\par\\nobreak"))
    else
      flush(block)
      blocks:insert(block)
    end
  end
  flush()
  doc.blocks = blocks
  return doc
end

return {
  {Str = text_symbols, Math = math_symbols, Table = table_widths},
  {traverse = "topdown", Header = function(h) return h, false end, Code = inline_code},
  {Pandoc = group_headings},
  {Header = deep_heading},
}
