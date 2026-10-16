import csv
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

def remove_duplicate_phones(input_file, output_file):
    """Remove rows with duplicate phone numbers, keeping the first occurrence."""
    print(f"Processing {input_file}...")
    
    # Read CSV
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        rows = list(reader)
    
    # Find phone columns (those containing 'Phone' and 'Value')
    if not rows:
        raise ValueError("CSV dosyasi bos.")
    header = rows[0]
    phone_columns = [i for i, col in enumerate(header) if 'Phone' in col and 'Value' in col]
    
    print(f"Found phone columns at indices: {phone_columns}")
    
    # Track seen phone numbers
    seen_phones = set()
    unique_rows = [header]  # Keep header
    
    for row in rows[1:]:  # Skip header
        # Get the first non-empty phone number
        phone = None
        for phone_col in phone_columns:
            if phone_col < len(row) and row[phone_col] and row[phone_col].strip():
                phone = row[phone_col].strip()
                break
        
        if not phone:
            continue
        
        # If phone not seen before, add to results
        if phone not in seen_phones:
            seen_phones.add(phone)
            unique_rows.append(row)
        else:
            print(f"Duplicate phone found and removed: {phone}")
    
    print(f"Original rows: {len(rows)}")
    print(f"Unique rows: {len(unique_rows)}")
    print(f"Removed duplicates: {len(rows) - len(unique_rows)}")
    
    # Save to output file
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(unique_rows)
    
    print(f"Saved deduplicated data to {output_file}")

if __name__ == "__main__":
    target = BASE / "ma_contacts2026_cleaned.csv"
    remove_duplicate_phones(target, target)
    print("\nDone! Duplicate phone numbers have been removed.")
