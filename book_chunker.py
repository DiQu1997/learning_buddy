import fitz  # PyMuPDF
import base64
import json
import os
import argparse
import random
from openai import OpenAI

# Configuration
MODEL_FAST = "gpt-5-mini"  # Cost-effective for scanning pages
MODEL_SMART = "gpt-5"      # Better for complex logic like splitting plans

# Load environment variables
from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/.env")) 

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
# ==============================================================================
# MODULE 1: PDF Utilities & Content Extraction
# ==============================================================================

def encode_image(pix):
    """Encodes a PyMuPDF pixmap to base64 string."""
    return base64.b64encode(pix.tobytes()).decode('utf-8')

def determine_pdf_mode(doc):
    """
    Verifies if the PDF is text-based or image-based.
    Checks a sample page from the middle of the book.
    """
    page_count = len(doc)
    check_page_idx = min(10, page_count // 2)
    page = doc.load_page(check_page_idx)
    text = page.get_text()
    
    # Heuristic: If text length is very low, assume image-based/scanned
    is_image_based = len(text.strip()) < 50
    
    print(f"[System] PDF Mode Analysis: {'Image-Based (Vision)' if is_image_based else 'Text-Based'}")
    return is_image_based

def get_page_content_for_llm(doc, page_idx, is_image_based):
    """
    Returns the content of a page in a format suitable for the LLM API.
    If text-based: returns string.
    If image-based: returns the image block structure for GPT Vision.
    """
    page = doc.load_page(page_idx)
    
    if is_image_based:
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2)) # 2x zoom for clarity
        base64_image = encode_image(pix)
        return [
            {"type": "text", "text": f"Page Index: {page_idx}."},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
            }
        ]
    else:
        text = page.get_text()
        # Add page index context so LLM knows where it is
        return f"Page Index: {page_idx}.\n\n{text}"

# ==============================================================================
# MODULE 2: Table of Contents (ToC) Locator
# ==============================================================================

def find_toc_range(doc, is_image_based):
    """
    Scans the book page-by-page to find the Start and End of the Table of Contents.
    """
    toc_start = None
    toc_end = None
    
    print("[System] Scanning for Table of Contents...")
    
    # Limit scan to first 50 pages to save tokens/time
    scan_limit = min(len(doc), 50) 
    
    for i in range(scan_limit):
        content = get_page_content_for_llm(doc, i, is_image_based)
        
        # Determine prompt based on whether we are looking for start or continuation
        if toc_start is None:
            system_prompt = (
                "You are a PDF navigator. Determine if the provided page is the START of the Table of Contents (ToC). "
                "The ToC must list chapters and page numbers. "
                "Ignore 'Contents in Brief' or short summaries. "
                "Reply ONLY with JSON: {\"is_toc_start\": true/false}"
            )
        else:
            system_prompt = (
                "You are a PDF navigator. We are currently inside the Table of Contents. "
                "Determine if this page continues the ToC or if the ToC has ended (e.g., we reached the Preface or Chapter 1). "
                "Reply ONLY with JSON: {\"is_toc_continue\": true/false}"
            )

        response = client.chat.completions.create(
            model=MODEL_SMART,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content}
            ],
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        
        if toc_start is None:
            if result.get("is_toc_start"):
                toc_start = i
                print(f"[System] ToC Start found at index {i}")
        else:
            # We are looking for the end
            if not result.get("is_toc_continue"):
                toc_end = i - 1 # Previous page was the last one
                print(f"[System] ToC End found at index {toc_end}")
                break
    
    if toc_start is not None and toc_end is None:
        # If we reached limit without ending, assume end is limit
        toc_end = scan_limit - 1

    if toc_start is None:
        raise Exception("Could not find Table of Contents in the first 50 pages.")

    return toc_start, toc_end

# ==============================================================================
# MODULE 3: ToC Extraction & Parsing
# ==============================================================================

def extract_toc_structure(doc, start, end, is_image_based):
    """
    Reads the identified ToC pages, converts to Markdown, then parses into structure.
    """
    print(f"[System] Extracting ToC content from pages {start} to {end}...")
    
    full_toc_context = ""
    
    # 1. Aggregate Content
    for i in range(start, end + 1):
        content = get_page_content_for_llm(doc, i, is_image_based)
        
        # If image based, we need to convert image to text description first for aggregation
        if is_image_based:
            response = client.chat.completions.create(
                model=MODEL_SMART,
                messages=[
                    {"role": "system", "content": "Transcribe the text on this Table of Contents page exactly into Markdown format. Maintain hierarchy."},
                    {"role": "user", "content": content}
                ]
            )
            text_content = response.choices[0].message.content
        else:
            text_content = content # It's already text

        full_toc_context += f"\n--- Page {i} ---\n{text_content}"

    # 2. Parse to Structure
    print("[System] Parsing ToC into structured JSON...")
    system_prompt = (
        "You are a structured data extractor. Convert the raw Table of Contents text into a strict JSON list. "
        "Each item should have: 'title', 'level' (1 for Chapter, 2 for Section), and 'page_number' (integer). "
        "Clean up the titles. If a page number is missing or Roman numeral, use null."
        "Example: {\"toc\": [{\"title\": \"Intro\", \"level\": 1, \"page_number\": 1}]}"
    )

    response = client.chat.completions.create(
        model=MODEL_SMART,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": full_toc_context}
        ],
        response_format={"type": "json_object"}
    )
    
    data = json.loads(response.choices[0].message.content)
    return data['toc']

# ==============================================================================
# MODULE 4: TOC Validation
# ==============================================================================

def validate_toc_page_numbers(toc_structure):
    """
    Deep validation of TOC page numbers using LLM-based analysis.
    Detects complex patterns like chaptered numbering (2-5, 5-4) that cannot be mapped to PDF pages.
    Returns (is_valid, reason, analysis).
    """
    if not toc_structure:
        return False, "Empty TOC structure", {}

    print("[System] Analyzing TOC page number format...")

    # Collect all page numbers for pattern analysis
    page_numbers = []
    for item in toc_structure:
        page_num = item.get('page_number')
        if page_num is not None:
            page_numbers.append({
                'title': item.get('title', 'Unknown'),
                'page_number': str(page_num)
            })

    if len(page_numbers) == 0:
        return False, "No page numbers found in TOC", {}

    # Use LLM to analyze the page number format
    analysis_prompt = {
        "instruction": (
            "Analyze these Table of Contents page numbers and determine if they can be directly mapped to PDF page indices.\n\n"
            "USABLE formats:\n"
            "- Simple integers: 1, 2, 3, 45, 123\n"
            "- Roman numerals at the start (can be handled): i, ii, iii, iv, then 1, 2, 3\n\n"
            "UNUSABLE formats (return false):\n"
            "- Chaptered numbering: 1-1, 1-5, 2-1, 2-8, 3-1 (chapter-page format)\n"
            "- Section-based: 5-4, 7-12 (section-page format)\n"
            "- Complex patterns that cannot map to sequential PDF pages\n\n"
            "Analyze the pattern and respond with JSON:\n"
            "{\n"
            "  \"is_usable\": true/false,\n"
            "  \"format_type\": \"simple_integers|roman_numerals|chaptered|section_based|mixed|unknown\",\n"
            "  \"confidence\": 0.0-1.0,\n"
            "  \"reasoning\": \"detailed explanation of the pattern detected\",\n"
            "  \"sample_pages\": [\"list of 3-5 example page numbers that show the pattern\"]\n"
            "}"
        ),
        "page_numbers": page_numbers[:20]  # Send first 20 entries for analysis
    }

    try:
        response = client.chat.completions.create(
            model=MODEL_SMART,  # Use smart model for critical decision
            messages=[
                {"role": "system", "content": "You are an expert at analyzing document structures and page numbering systems."},
                {"role": "user", "content": json.dumps(analysis_prompt, indent=2)}
            ],
            response_format={"type": "json_object"}
        )

        analysis = json.loads(response.choices[0].message.content)

        is_usable = analysis.get('is_usable', False)
        format_type = analysis.get('format_type', 'unknown')
        confidence = analysis.get('confidence', 0.0)
        reasoning = analysis.get('reasoning', 'No reasoning provided')

        print(f"[System] Format Type: {format_type}")
        print(f"[System] Confidence: {confidence:.0%}")
        print(f"[System] Reasoning: {reasoning}")

        if not is_usable:
            return False, f"TOC page numbers are not usable - {format_type}: {reasoning}", analysis

        if confidence < 0.7:
            return False, f"Low confidence ({confidence:.0%}) in TOC page number format", analysis

        return True, f"TOC page numbers are usable ({format_type})", analysis

    except Exception as e:
        print(f"[Warning] LLM analysis failed: {e}")
        # Fallback to simple pattern detection
        return _simple_page_number_validation(page_numbers)

def _simple_page_number_validation(page_numbers):
    """
    Fallback simple pattern validation when LLM analysis fails.
    """
    if len(page_numbers) == 0:
        return False, "No page numbers found", {}

    # Check for chaptered patterns
    chaptered_count = 0
    simple_int_count = 0

    for item in page_numbers:
        page_str = item['page_number']
        if '-' in page_str and page_str.replace('-', '').isdigit():
            chaptered_count += 1
        elif page_str.isdigit():
            simple_int_count += 1

    total = len(page_numbers)

    if chaptered_count > total * 0.3:  # More than 30% chaptered
        return False, f"Detected chaptered page numbering ({chaptered_count}/{total} entries)", {
            'format_type': 'chaptered',
            'chaptered_count': chaptered_count,
            'total': total
        }

    if simple_int_count < 3:
        return False, f"Too few simple integer page numbers ({simple_int_count}/{total})", {}

    return True, "Simple integer page numbers detected", {'format_type': 'simple_integers'}

def validate_toc_offset_accuracy(doc, toc_structure, toc_end_idx, offset, is_image_based):
    """
    Comprehensive validation of TOC offset with strategic sampling.
    Samples from beginning, middle, and end of the TOC to ensure accuracy.
    Returns (is_accurate, confidence_score, details).
    """
    print("[System] Validating TOC offset accuracy with comprehensive sampling...")

    # Get chapters with valid integer page numbers
    valid_chapters = []
    for item in toc_structure:
        page_num = item.get('page_number')
        if page_num and str(page_num).isdigit():
            valid_chapters.append(item)

    if not valid_chapters:
        return False, 0.0, {'error': 'No valid chapters to test'}

    total_chapters = len(valid_chapters)

    # Strategic sampling: beginning, middle, end
    test_chapters = []

    # Sample from beginning (first 3 chapters)
    beginning_sample = valid_chapters[:min(3, total_chapters)]
    test_chapters.extend(beginning_sample)

    # Sample from middle (2-3 chapters)
    if total_chapters > 6:
        middle_start = total_chapters // 3
        middle_end = (total_chapters * 2) // 3
        middle_chapters = valid_chapters[middle_start:middle_end]
        if len(middle_chapters) > 3:
            middle_sample = random.sample(middle_chapters, min(3, len(middle_chapters)))
        else:
            middle_sample = middle_chapters
        test_chapters.extend(middle_sample)

    # Sample from end (last 2-3 chapters)
    if total_chapters > 3:
        end_sample = valid_chapters[-min(3, total_chapters):]
        test_chapters.extend(end_sample)

    # Remove duplicates while preserving order
    seen = set()
    test_chapters = [ch for ch in test_chapters if not (ch['title'] in seen or seen.add(ch['title']))]

    # Limit to max 8 tests to control API costs
    test_chapters = test_chapters[:8]

    print(f"[System] Testing {len(test_chapters)} chapters (from beginning, middle, and end)...")

    matches = 0
    total_tests = len(test_chapters)
    test_results = []

    for chapter in test_chapters:
        target_title = chapter['title']
        target_page = int(chapter['page_number'])
        physical_idx = target_page + offset

        # Check if physical index is valid
        if physical_idx < 0 or physical_idx >= len(doc):
            test_results.append({
                'title': target_title,
                'target_page': target_page,
                'physical_idx': physical_idx,
                'found': False,
                'reason': 'out_of_bounds'
            })
            continue

        found = False
        found_at = None

        # Check a range around the target (±3 pages for more tolerance)
        for offset_check in range(-3, 4):
            check_idx = physical_idx + offset_check
            if check_idx < 0 or check_idx >= len(doc):
                continue

            content = get_page_content_for_llm(doc, check_idx, is_image_based)

            system_prompt = (
                f"Does this page contain or start the chapter/section titled '{target_title}'? "
                "Look for the exact title or very close variation. "
                "Reply ONLY with JSON: {\"is_match\": true/false, \"reasoning\": \"brief explanation\"}"
            )

            try:
                response = client.chat.completions.create(
                    model=MODEL_FAST,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": content}
                    ],
                    response_format={"type": "json_object"}
                )

                result = json.loads(response.choices[0].message.content)

                if result.get("is_match"):
                    found = True
                    found_at = check_idx
                    matches += 1
                    print(f"[System] ✓ Found '{target_title}' at physical page {check_idx} (expected {physical_idx})")
                    break
            except Exception as e:
                print(f"[Warning] Check failed for '{target_title}': {e}")
                continue

        test_results.append({
            'title': target_title,
            'target_page': target_page,
            'physical_idx': physical_idx,
            'found': found,
            'found_at': found_at,
            'reason': 'found' if found else 'not_found'
        })

        if not found:
            print(f"[System] ✗ Could not find '{target_title}' near expected location (page {physical_idx})")

    confidence = matches / total_tests if total_tests > 0 else 0.0
    is_accurate = confidence >= 0.65  # Require 65% accuracy (more strict)

    print(f"\n[System] TOC Offset Validation Results:")
    print(f"  - Tests: {total_tests} chapters sampled")
    print(f"  - Matches: {matches} found")
    print(f"  - Confidence: {confidence:.1%}")
    print(f"  - Status: {'PASSED ✓' if is_accurate else 'FAILED ✗'}")

    details = {
        'matches': matches,
        'total_tests': total_tests,
        'confidence': confidence,
        'test_results': test_results
    }

    return is_accurate, confidence, details

# ==============================================================================
# MODULE 5: Offset Calculation
# ==============================================================================

def calculate_offset_by_anchor(doc, toc_structure, toc_end_idx, is_image_based):
    """
    Calculates offset by finding the physical location of the first major ToC entry.
    Universal formula: Offset = Physical_Index_Found - ToC_Listed_Page
    """
    print("[System] Calculating page offset using Anchor Matching...")

    # 1. Select a valid Anchor from ToC
    # We look for the first entry that looks like a main chapter (has a digit page number)
    anchor = None
    for item in toc_structure:
        # Simple check: page number must be a digit (ignore 'iii', 'xx') and title distinct
        if item['page_number'] and str(item['page_number']).isdigit():
            anchor = item
            break
    
    if not anchor:
        print("[Warning] No valid integer page numbers found in ToC. Defaulting offset to 0.")
        return 0

    target_title = anchor['title']
    target_page_str = str(anchor['page_number'])
    target_page_int = int(anchor['page_number'])
    
    print(f"[System] Selected Anchor: '{target_title}' (Listed Page: {target_page_str})")
    
    # 2. Search for this Anchor physically
    # We start searching immediately after the ToC ends.
    # We limit search to 50 pages to prevent reading the whole book if parsing fails.
    start_search = toc_end_idx + 1
    search_limit = min(len(doc), start_search + 50)
    
    found_physical_index = None

    for i in range(start_search, search_limit):
        content = get_page_content_for_llm(doc, i, is_image_based)
        
        # We ask LLM if this page is the start of that specific chapter
        system_prompt = (
            f"You are checking if this page is the start of the chapter: '{target_title}'. \n"
            "Criteria:\n"
            "1. The title must be prominent.\n"
            "2. It should look like the beginning of a section.\n"
            "3. If the page contains only a continued discussion, it is NOT the start.\n"
            "Reply ONLY with JSON: {\"is_match\": true/false}"
        )
        
        response = client.chat.completions.create(
            model=MODEL_FAST,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content}
            ],
            response_format={"type": "json_object"}
        )
        
        result = json.loads(response.choices[0].message.content)
        
        if result.get("is_match"):
            found_physical_index = i
            print(f"[System] Anchor '{target_title}' found at physical index {found_physical_index}")
            break
    
    # 3. Calculate Offset
    if found_physical_index is not None:
        # THE GOLDEN FORMULA
        offset = found_physical_index - target_page_int
        print(f"[System] Logic: Found Index ({found_physical_index}) - Listed Page ({target_page_int}) = Offset ({offset})")
        return offset
    else:
        # Fallback: If we can't find the chapter, we might assume the page *after* ToC 
        # is the page number listed in the first ToC entry (Best Guess).
        print(f"[System] Warning: Could not physically find anchor '{target_title}'. Guessing based on ToC end.")
        # Guess: Physical location is (toc_end + 1), so offset is (toc_end + 1) - target_page
        guessed_physical = toc_end_idx + 1
        offset = guessed_physical - target_page_int
        return offset

# ==============================================================================
# MODULE 6: Sequential Reading Agent (Fallback Strategy)
# ==============================================================================

def detect_chapter_boundary(doc, current_idx, chapter_context, is_image_based):
    """
    Analyzes if the current page marks a new chapter boundary.
    Returns (is_new_chapter, chapter_info).
    """
    content = get_page_content_for_llm(doc, current_idx, is_image_based)

    system_prompt = (
        "You are analyzing a page to determine if it starts a NEW chapter or major section. "
        "Context: " + json.dumps(chapter_context) + "\n\n"
        "Analyze this page and determine:\n"
        "1. Does this page start a NEW chapter/section? (not just continue the previous one)\n"
        "2. If yes, what is the chapter title and number (if visible)?\n"
        "3. Is this a significant break (new chapter) or minor break (subsection)?\n\n"
        "Reply ONLY with JSON:\n"
        "{\n"
        "  \"is_new_chapter\": true/false,\n"
        "  \"chapter_title\": \"title or null\",\n"
        "  \"chapter_number\": \"number or null\",\n"
        "  \"significance\": \"major/minor/none\",\n"
        "  \"reasoning\": \"brief explanation\"\n"
        "}"
    )

    try:
        response = client.chat.completions.create(
            model=MODEL_FAST,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content}
            ],
            response_format={"type": "json_object"}
        )

        result = json.loads(response.choices[0].message.content)
        return result.get('is_new_chapter', False), result

    except Exception as e:
        print(f"[Warning] Error detecting chapter boundary at page {current_idx}: {e}")
        return False, {}

def sequential_reading_agent(doc, is_image_based, start_idx=0, max_pages_per_part=35):
    """
    Sequential reading agent that reads page-by-page to detect chapters.
    Returns a list of parts with their page ranges.
    """
    print("[System] Starting Sequential Reading Agent...")
    print("[System] This will read the document page-by-page to detect chapter boundaries.")

    parts = []
    current_part_start = start_idx
    current_chapter_info = {
        "title": "Introduction/Start",
        "number": "1",
        "start_page": start_idx
    }

    total_pages = len(doc)
    pages_in_current_part = 0

    # Skip TOC pages and front matter - start from a reasonable point
    if start_idx == 0:
        # Quick scan to skip front matter
        print("[System] Scanning for content start...")
        for i in range(min(30, total_pages)):
            content = get_page_content_for_llm(doc, i, is_image_based)

            system_prompt = (
                "Is this page part of the main content (Chapter 1 or actual content), "
                "or is it still front matter (title page, TOC, preface, acknowledgments)? "
                "Reply ONLY with JSON: {\"is_main_content\": true/false}"
            )

            try:
                response = client.chat.completions.create(
                    model=MODEL_FAST,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": content}
                    ],
                    response_format={"type": "json_object"}
                )

                result = json.loads(response.choices[0].message.content)
                if result.get("is_main_content"):
                    start_idx = i
                    current_part_start = i
                    current_chapter_info["start_page"] = i
                    print(f"[System] Main content starts at page {i}")
                    break
            except:
                continue

    print(f"[System] Reading from page {start_idx} to {total_pages}...")

    # Read through the document
    i = start_idx
    while i < total_pages:
        # Check if we should break based on page limit
        if pages_in_current_part >= max_pages_per_part:
            # Force a break here
            parts.append({
                "part_title": current_chapter_info.get("title", f"Part {len(parts) + 1}"),
                "start_page": current_part_start,
                "end_page": i - 1,
                "description": f"Pages {current_part_start} to {i-1}"
            })

            print(f"[System] Part {len(parts)}: {current_chapter_info.get('title')} "
                  f"(Pages {current_part_start}-{i-1}, {pages_in_current_part} pages)")

            current_part_start = i
            pages_in_current_part = 0

        # Detect chapter boundary every 5 pages or when approaching limit
        should_check = (i - current_part_start) % 5 == 0 or pages_in_current_part >= max_pages_per_part - 5

        if should_check:
            is_new_chapter, chapter_info = detect_chapter_boundary(doc, i, current_chapter_info, is_image_based)

            if is_new_chapter and chapter_info.get('significance') == 'major':
                # We found a major chapter break
                # Check if current part is big enough (at least 10 pages or it's the first part)
                if pages_in_current_part >= 10 or len(parts) == 0:
                    # Save the current part
                    parts.append({
                        "part_title": current_chapter_info.get("title", f"Part {len(parts) + 1}"),
                        "start_page": current_part_start,
                        "end_page": i - 1,
                        "description": f"Pages {current_part_start} to {i-1}"
                    })

                    print(f"[System] Part {len(parts)}: {current_chapter_info.get('title')} "
                          f"(Pages {current_part_start}-{i-1}, {pages_in_current_part} pages)")

                    # Start new part
                    current_part_start = i
                    current_chapter_info = {
                        "title": chapter_info.get('chapter_title', f"Chapter {chapter_info.get('chapter_number', '?')}"),
                        "number": chapter_info.get('chapter_number'),
                        "start_page": i
                    }
                    pages_in_current_part = 0

        pages_in_current_part += 1
        i += 1

    # Add the final part
    if pages_in_current_part > 0:
        parts.append({
            "part_title": current_chapter_info.get("title", f"Part {len(parts) + 1}"),
            "start_page": current_part_start,
            "end_page": total_pages - 1,
            "description": f"Pages {current_part_start} to {total_pages - 1}"
        })

        print(f"[System] Part {len(parts)}: {current_chapter_info.get('title')} "
              f"(Pages {current_part_start}-{total_pages - 1}, {pages_in_current_part} pages)")

    return parts

def execute_sequential_split(doc, parts, output_dir="book_parts"):
    """
    Executes PDF split based on sequential reading results.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    print(f"[System] Splitting PDF into {len(parts)} parts...")

    for i, part in enumerate(parts):
        # Handle None title safely
        raw_title = part.get('part_title') or f"Part_{i+1}"
        title = raw_title.replace(" ", "_").replace("/", "-").replace("\\", "-")
        start_idx = part.get('start_page', 0)
        end_idx = part.get('end_page', start_idx)

        try:
            new_doc = fitz.open()
            new_doc.insert_pdf(doc, from_page=start_idx, to_page=end_idx)

            filename = f"{output_dir}/Part_{i+1:02d}_{title}.pdf"
            new_doc.save(filename)
            new_doc.close()

            print(f"Created: {filename} (Physical pages {start_idx}-{end_idx})")

        except Exception as e:
            print(f"[Error] Failed to create '{title}': {e}")

# ==============================================================================
# MODULE 7: Split Planning (TOC-based)
# ==============================================================================

def generate_split_plan(toc_structure, total_physical_pages, offset, max_pages_per_part=40):
    """
    Uses LLM to generate a smart split plan based on ToC and constraints.
    """
    print("[System] Generating Split Plan...")
    
    prompt_context = {
        "toc": toc_structure,
        "total_pdf_pages": total_physical_pages,
        "offset": offset,
        "max_pages_target": max_pages_per_part
    }
    
    system_prompt = (
        "You are an expert curriculum planner. I have a book ToC and need to split it into learnable 'Parts'. "
        "Rules:\n"
        "1. Each Part should target approximately the 'max_pages_target'.\n"
        "2. Keep chapters whole if they fit. Do not split a small chapter just to fill a Part.\n"
        "3. If a chapter is huge (e.g., 80 pages) and max is 40, split it into 'Chapter X Part 1' and 'Chapter X Part 2'.\n"
        "4. Return a JSON list of parts. Each part has: 'part_title', 'start_printed_page', 'end_printed_page', 'description'.\n"
        "5. Ensure continuity: Part 2 start page should be Part 1 end page + 1.\n"
        "6. Handle the end of the book correctly using the last known page numbers."
    )

    response = client.chat.completions.create(
        model=MODEL_SMART,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(prompt_context)}
        ],
        response_format={"type": "json_object"}
    )
    
    plan = json.loads(response.choices[0].message.content)
    return plan.get("parts", [])

# ==============================================================================
# MODULE 8: Adaptive Strategy Selector
# ==============================================================================

def choose_chunking_strategy(doc, is_image_based, max_pages_per_part=35):
    """
    Intelligently chooses between TOC-based and Sequential reading strategies.
    Returns (strategy, data) where strategy is 'toc' or 'sequential'.
    """
    print("\n" + "="*70)
    print("ADAPTIVE CHUNKING STRATEGY SELECTOR")
    print("="*70)

    try:
        # Step 1: Try to find TOC
        print("\n[Step 1] Attempting to locate Table of Contents...")
        toc_start, toc_end = find_toc_range(doc, is_image_based)
        print(f"[Step 1] ✓ TOC found: pages {toc_start} to {toc_end}")

        # Step 2: Extract TOC structure
        print("\n[Step 2] Extracting TOC structure...")
        toc_structure = extract_toc_structure(doc, toc_start, toc_end, is_image_based)
        print(f"[Step 2] ✓ Extracted {len(toc_structure)} TOC entries")

        # Step 3: Validate TOC page numbers
        print("\n[Step 3] Validating TOC page number format...")
        is_valid, reason, analysis = validate_toc_page_numbers(toc_structure)
        print(f"[Step 3] {'✓' if is_valid else '✗'} {reason}")

        if not is_valid:
            print(f"\n[Decision] TOC page numbers are not usable: {reason}")
            if analysis.get('format_type') == 'chaptered':
                print(f"[Decision] Detected chaptered numbering (e.g., 1-1, 2-5) which cannot map to PDF pages")
            print("[Decision] Falling back to SEQUENTIAL READING strategy")
            return 'sequential', {'doc': doc, 'is_image_based': is_image_based}

        # Step 4: Calculate offset
        print("\n[Step 4] Calculating page offset...")
        offset = calculate_offset_by_anchor(doc, toc_structure, toc_end, is_image_based)
        print(f"[Step 4] ✓ Calculated offset: {offset}")

        # Step 5: Validate offset accuracy
        print("\n[Step 5] Validating offset accuracy...")
        is_accurate, confidence, details = validate_toc_offset_accuracy(doc, toc_structure, toc_end, offset, is_image_based)

        if not is_accurate:
            print(f"\n[Decision] TOC offset validation failed (confidence: {confidence:.1%})")
            print(f"[Decision] Only {details.get('matches', 0)}/{details.get('total_tests', 0)} test chapters matched expected locations")
            print("[Decision] Falling back to SEQUENTIAL READING strategy")
            return 'sequential', {'doc': doc, 'is_image_based': is_image_based}

        # Step 6: TOC is good - use TOC-based strategy
        print(f"\n[Decision] ✓ TOC is reliable (confidence: {confidence:.1%})")
        print("[Decision] Using TOC-BASED chunking strategy")

        return 'toc', {
            'toc_structure': toc_structure,
            'offset': offset,
            'total_pages': len(doc)
        }

    except Exception as e:
        # If anything goes wrong, fall back to sequential
        print(f"\n[Error] TOC processing failed: {e}")
        print("[Decision] Falling back to SEQUENTIAL READING strategy")
        return 'sequential', {'doc': doc, 'is_image_based': is_image_based}

# ==============================================================================
# MODULE 9: Execution (PDF Splitting)
# ==============================================================================

def execute_split(doc, plan, offset, output_dir="book_parts"):
    """
    Physically splits the PDF based on the plan and offset.
    Includes robust error checking for page ranges.
    """
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        
    print(f"[System] Splitting PDF into {len(plan)} parts...")
    
    total_pages = len(doc)

    for i, part in enumerate(plan):
        # Handle None title safely
        raw_title = part.get('part_title') or f"Part_{i+1}"
        title = raw_title.replace(" ", "_").replace("/", "-").replace("\\", "-")

        # safely get page numbers, defaulting to None if missing
        start_printed = part.get('start_printed_page')
        end_printed = part.get('end_printed_page')

        # Skip parts with missing page info
        if start_printed is None or end_printed is None:
            print(f"[Skipping] Part '{title}' missing page numbers.")
            continue

        try:
            # Ensure we are working with integers (handle strings from JSON)
            start_printed_int = int(start_printed)
            end_printed_int = int(end_printed)
            
            # Calculate physical indices
            start_idx = start_printed_int + offset
            end_idx = end_printed_int + offset
            
            # Strict Boundary Clamping
            # PyMuPDF fails if ranges are outside (0, total_pages - 1)
            start_idx = max(0, start_idx)
            end_idx = min(total_pages - 1, end_idx)
            
            # Logic check: Start must be before End
            if start_idx > end_idx:
                print(f"[Skipping] Invalid range for '{title}': Physical {start_idx}-{end_idx}")
                continue
                
            # Create the new PDF for this part
            new_doc = fitz.open()
            
            # insert_pdf expects: from_page (inclusive), to_page (inclusive)
            new_doc.insert_pdf(doc, from_page=start_idx, to_page=end_idx)
            
            filename = f"{output_dir}/Part_{i+1:02d}_{title}.pdf"
            new_doc.save(filename)
            new_doc.close()
            
            print(f"Created: {filename} (Pages {start_printed}-{end_printed})")
            
        except ValueError:
            # Catches errors if 'start_printed' is a Roman numeral (e.g., 'ix') or text
            print(f"[Warning] Could not parse page numbers for '{title}': {start_printed}-{end_printed}")
        except Exception as e:
            print(f"[Error] Failed to create '{title}': {e}")

# ==============================================================================
# MAIN ORCHESTRATOR
# ==============================================================================

def process_book(pdf_path, max_pages_per_part=35, output_dir="book_parts", force_strategy=None):
    """
    Main orchestrator with adaptive strategy selection.

    Args:
        pdf_path: Path to the PDF file
        max_pages_per_part: Maximum pages per part (default: 35)
        output_dir: Output directory (default: "book_parts")
        force_strategy: Force a specific strategy ('toc' or 'sequential'), or None for auto-selection
    """
    try:
        doc = fitz.open(pdf_path)
        print(f"Opened {pdf_path} with {len(doc)} pages.")

        # 1. Check Mode (text-based or image-based)
        is_image_based = determine_pdf_mode(doc)

        # 2. Choose Strategy
        if force_strategy:
            print(f"\n[System] Forced strategy: {force_strategy.upper()}")
            if force_strategy == 'sequential':
                strategy = 'sequential'
                strategy_data = {'doc': doc, 'is_image_based': is_image_based}
            else:
                strategy = 'toc'
                # Still need to extract TOC for forced TOC strategy
                toc_start, toc_end = find_toc_range(doc, is_image_based)
                toc_structure = extract_toc_structure(doc, toc_start, toc_end, is_image_based)
                offset = calculate_offset_by_anchor(doc, toc_structure, toc_end, is_image_based)
                strategy_data = {
                    'toc_structure': toc_structure,
                    'offset': offset,
                    'total_pages': len(doc)
                }
        else:
            strategy, strategy_data = choose_chunking_strategy(doc, is_image_based, max_pages_per_part)

        # 3. Execute based on chosen strategy
        print("\n" + "="*70)
        print(f"EXECUTING {strategy.upper()} STRATEGY")
        print("="*70 + "\n")

        if strategy == 'toc':
            # TOC-based approach
            plan = generate_split_plan(
                strategy_data['toc_structure'],
                strategy_data['total_pages'],
                strategy_data['offset'],
                max_pages_per_part
            )
            print(f"[System] Generated split plan with {len(plan)} parts")
            execute_split(doc, plan, strategy_data['offset'], output_dir)

        else:
            # Sequential reading approach
            parts = sequential_reading_agent(
                strategy_data['doc'],
                strategy_data['is_image_based'],
                start_idx=0,
                max_pages_per_part=max_pages_per_part
            )
            print(f"[System] Detected {len(parts)} parts through sequential reading")
            execute_sequential_split(doc, parts, output_dir)

        print("\n" + "="*70)
        print(f"SUCCESS: Book processing complete!")
        print(f"Output directory: {output_dir}")
        print("="*70)

    except Exception as e:
        print(f"\n[Error] {e}")
        import traceback
        traceback.print_exc()

# ==============================================================================
# ENTRY POINT
# ==============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Intelligent PDF book chunker with adaptive strategy selection.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
The chunker automatically selects the best strategy:
  - TOC-based: Uses Table of Contents if reliable page numbers are found
  - Sequential: Reads page-by-page to detect chapters (fallback for complex PDFs)

Examples:
  %(prog)s book.pdf
  %(prog)s book.pdf --max-pages 30
  %(prog)s book.pdf --max-pages 40 --output-dir my_book_parts
  %(prog)s book.pdf --strategy sequential  # Force sequential reading
  %(prog)s book.pdf --strategy toc         # Force TOC-based chunking
        """
    )

    parser.add_argument(
        "pdf_file",
        help="Path to the PDF file to process"
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=35,
        help="Maximum pages per part (default: 35)"
    )

    parser.add_argument(
        "--output-dir",
        default="book_parts",
        help="Output directory for split PDF parts (default: book_parts)"
    )

    parser.add_argument(
        "--strategy",
        choices=['auto', 'toc', 'sequential'],
        default='auto',
        help="Chunking strategy: 'auto' (intelligent selection), 'toc' (use Table of Contents), "
             "'sequential' (page-by-page reading). Default: auto"
    )

    args = parser.parse_args()

    # Validate PDF file exists
    if not os.path.exists(args.pdf_file):
        print(f"Error: File not found: {args.pdf_file}")
        exit(1)

    # Determine strategy
    force_strategy = None if args.strategy == 'auto' else args.strategy

    # Process the book
    process_book(
        args.pdf_file,
        max_pages_per_part=args.max_pages,
        output_dir=args.output_dir,
        force_strategy=force_strategy
    )