import csv
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

def remove_empty_phone_rows(input_file, output_file):
    """Remove rows where all phone number values are empty."""
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
    
    # Keep header and rows with at least one non-empty phone value
    filtered_rows = [rows[0]]  # Keep header
    
    for row in rows[1:]:  # Skip header
        has_phone = False
        for col_idx in phone_columns:
            if col_idx < len(row) and row[col_idx] and row[col_idx].strip():
                has_phone = True
                break
        
        if has_phone:
            filtered_rows.append(row)
    
    print(f"Original rows: {len(rows)}")
    print(f"Filtered rows: {len(filtered_rows)}")
    print(f"Removed rows: {len(rows) - len(filtered_rows)}")
    
    # Save to output file
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(filtered_rows)
    
    print(f"Saved filtered data to {output_file}")

if __name__ == "__main__":
    target = BASE / "ma_contacts2026_cleaned.csv"
    remove_empty_phone_rows(target, target)
    print("\nDone! Empty phone rows have been removed.")
