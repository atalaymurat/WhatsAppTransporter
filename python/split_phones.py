import csv
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

def split_phone_rows(input_file, output_file):
    """Split rows with multiple phone numbers into separate rows."""
    print(f"Processing {input_file}...")
    
    # Read CSV
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        rows = list(reader)
    
    # Find phone columns (those containing 'Phone' and 'Value')
    if not rows:
        raise ValueError("CSV dosyasi bos.")
    header = rows[0]
    phone_columns = []
    phone_label_columns = []
    
    for i, col in enumerate(header):
        if 'Phone' in col and 'Value' in col:
            phone_columns.append(i)
        if 'Phone' in col and 'Label' in col:
            phone_label_columns.append(i)
    
    print(f"Found phone value columns at indices: {phone_columns}")
    print(f"Found phone label columns at indices: {phone_label_columns}")
    
    # Create new rows
    new_rows = [header]  # Keep header
    
    for row in rows[1:]:  # Skip header
        # Find all non-empty phone numbers
        phone_entries = []
        for idx, phone_col in enumerate(phone_columns):
            label_col = phone_label_columns[idx] if idx < len(phone_label_columns) else -1
            
            if phone_col < len(row) and row[phone_col] and row[phone_col].strip():
                label = row[label_col] if label_col < len(row) else ''
                phone_value = row[phone_col]
                
                # Check if multiple phones are separated by ::: or concatenated with +
                # Pattern: +90XXX+90XXX or similar
                # Split by ::: or by + followed by digits (but not the first +)
                if ':::' in phone_value:
                    phones = phone_value.split(':::')
                    for p in phones:
                        if p.strip():
                            phone_entries.append((phone_col, label_col, label, p.strip()))
                else:
                    # Try to split concatenated phone numbers like +90530+90531
                    # Look for pattern: +90\d{10}\+90\d{10}
                    # Split on + that comes after 10 digits
                    parts = re.split(r'(?<=\d{10})\+', phone_value)
                    if len(parts) > 1:
                        for p in parts:
                            if p.strip():
                                # Add + back if it was removed
                                if not p.startswith('+'):
                                    p = '+' + p
                                phone_entries.append((phone_col, label_col, label, p.strip()))
                    else:
                        phone_entries.append((phone_col, label_col, label, phone_value))
        
        # If no phones, skip row
        if not phone_entries:
            continue
        
        # Create separate row for each phone
        for source_phone_col, source_label_col, label, phone in phone_entries:
            new_row = row.copy()
            
            # Clear all phone columns
            for phone_col in phone_columns:
                if phone_col < len(new_row):
                    new_row[phone_col] = ''
            for label_col in phone_label_columns:
                if label_col < len(new_row):
                    new_row[label_col] = ''
            
            # Set only the current phone
            new_row[source_phone_col] = phone
            if 0 <= source_label_col < len(new_row):
                new_row[source_label_col] = label
            
            new_rows.append(new_row)
    
    print(f"Original rows: {len(rows)}")
    print(f"New rows: {len(new_rows)}")
    print(f"Added rows: {len(new_rows) - len(rows)}")
    
    # Save to output file
    with open(output_file, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerows(new_rows)
    
    print(f"Saved split data to {output_file}")

if __name__ == "__main__":
    target = BASE / "ma_contacts2026_cleaned.csv"
    split_phone_rows(target, target)
    print("\nDone! Multiple phone rows have been split.")
