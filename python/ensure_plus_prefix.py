import csv
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

def ensure_plus_prefix(input_file, output_file):
    """Ensure all phone numbers start with +."""
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
    
    # Update phone numbers
    updated_count = 0
    for row in rows[1:]:  # Skip header
        for phone_col in phone_columns:
            if phone_col < len(row) and row[phone_col] and row[phone_col].strip():
                phone = row[phone_col].strip()
                # If doesn't start with +, add it
                if not phone.startswith('+'):
                    row[phone_col] = '+' + phone
                    updated_count += 1
    
    print(f"Updated {updated_count} phone numbers with + prefix")
    
    # Save to output file
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    
    print(f"Saved updated data to {output_file}")

if __name__ == "__main__":
    target = BASE / "ma_contacts2026_cleaned.csv"
    ensure_plus_prefix(target, target)
    print("\nDone! All phone numbers now have + prefix.")
